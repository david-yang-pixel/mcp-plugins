"""Minimal PostgreSQL lexer used by the read-only SQL validator.

It only needs to know where comments, string literals, quoted identifiers and
dollar-quoted bodies start and end, so that semicolons / keywords / parentheses
can be judged on real SQL code instead of on raw text. Anything that is not
recognised becomes a single-character ``punct`` token.

Assumes ``standard_conforming_strings = on`` (PostgreSQL default since 9.1):
backslashes only escape inside ``E'...'`` strings.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


class SqlLexError(ValueError):
    """Raised for unterminated strings, identifiers, comments or dollar quotes."""


@dataclass(frozen=True)
class Token:
    kind: str  # space | comment | string | qident | word | punct
    text: str
    start: int

    @property
    def end(self) -> int:
        return self.start + len(self.text)

    @property
    def is_trivia(self) -> bool:
        return self.kind in ("space", "comment")


_WORD_START = re.compile(r"[A-Za-z_\u0080-\U0010ffff]")
_WORD = re.compile(r"[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_$\u0080-\U0010ffff]*")
_NUMBER = re.compile(r"[0-9][0-9A-Za-z_.]*|\.[0-9][0-9A-Za-z_.]*")
_SPACE = re.compile(r"\s+")
_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\u0080-\U0010ffff][A-Za-z0-9_\u0080-\U0010ffff]*)?\$")
# Prefixes that may be glued to a quote: E'..', B'..', X'..', N'..', U&'..', U&"..".
_STRING_PREFIXES = {"E", "B", "X", "N"}


def _scan_quoted(sql: str, pos: int, quote: str, *, backslash_escapes: bool) -> int:
    """Return index just past the closing quote starting at ``sql[pos] == quote``."""
    i = pos + 1
    n = len(sql)
    while i < n:
        ch = sql[i]
        if backslash_escapes and ch == "\\":
            i += 2
            continue
        if ch == quote:
            if i + 1 < n and sql[i + 1] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    kind = "字符串常量" if quote == "'" else "双引号标识符"
    raise SqlLexError(f"SQL 中存在未闭合的{kind}（从第 {pos + 1} 个字符开始）")


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch.isspace():
            m = _SPACE.match(sql, i)
            assert m is not None
            tokens.append(Token("space", m.group(0), i))
            i = m.end()
            continue
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            tokens.append(Token("comment", sql[i:j], i))
            i = j
            continue
        if sql.startswith("/*", i):
            depth = 0
            j = i
            while j < n:
                if sql.startswith("/*", j):
                    depth += 1
                    j += 2
                elif sql.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            if depth != 0:
                raise SqlLexError(f"SQL 中存在未闭合的块注释 /*（从第 {i + 1} 个字符开始）")
            tokens.append(Token("comment", sql[i:j], i))
            i = j
            continue
        if ch == "'":
            j = _scan_quoted(sql, i, "'", backslash_escapes=False)
            tokens.append(Token("string", sql[i:j], i))
            i = j
            continue
        if ch == '"':
            j = _scan_quoted(sql, i, '"', backslash_escapes=False)
            tokens.append(Token("qident", sql[i:j], i))
            i = j
            continue
        if ch == "$":
            prev_is_word = bool(tokens) and tokens[-1].kind == "word" and tokens[-1].end == i
            m = _DOLLAR_TAG.match(sql, i)
            if m and not prev_is_word:
                tag = m.group(0)
                close = sql.find(tag, m.end())
                if close < 0:
                    raise SqlLexError(
                        f"SQL 中存在未闭合的美元引号 {tag}（从第 {i + 1} 个字符开始）"
                    )
                j = close + len(tag)
                tokens.append(Token("string", sql[i:j], i))
                i = j
                continue
        if _WORD_START.match(ch):
            m = _WORD.match(sql, i)
            assert m is not None
            word = m.group(0)
            j = m.end()
            upper = word.upper()
            if upper in _STRING_PREFIXES and j < n and sql[j] == "'":
                end = _scan_quoted(sql, j, "'", backslash_escapes=(upper == "E"))
                tokens.append(Token("string", sql[i:end], i))
                i = end
                continue
            if upper == "U" and sql.startswith("&'", j):
                end = _scan_quoted(sql, j + 1, "'", backslash_escapes=False)
                tokens.append(Token("string", sql[i:end], i))
                i = end
                continue
            if upper == "U" and sql.startswith('&"', j):
                end = _scan_quoted(sql, j + 1, '"', backslash_escapes=False)
                tokens.append(Token("qident", sql[i:end], i))
                i = end
                continue
            tokens.append(Token("word", word, i))
            i = j
            continue
        if ch.isdigit() or (ch == "." and i + 1 < n and sql[i + 1].isdigit()):
            m = _NUMBER.match(sql, i)
            assert m is not None
            tokens.append(Token("word", m.group(0), i))
            i = m.end()
            continue
        tokens.append(Token("punct", ch, i))
        i += 1
    return tokens


def significant(tokens: list[Token]) -> list[Token]:
    return [t for t in tokens if not t.is_trivia]


def strip_statement_terminators(sql: str, tokens: list[Token]) -> tuple[str, list[Token]]:
    """Drop leading/trailing whitespace, comments and trailing semicolons.

    Returns the statement body (a slice of the original text) and its tokens.
    """
    start = 0
    end = len(tokens)
    while start < end and tokens[start].is_trivia:
        start += 1
    while end > start and (
        tokens[end - 1].is_trivia
        or (tokens[end - 1].kind == "punct" and tokens[end - 1].text == ";")
    ):
        end -= 1
    body_tokens = tokens[start:end]
    if not body_tokens:
        return "", []
    body = sql[body_tokens[0].start:body_tokens[-1].end]
    offset = body_tokens[0].start
    rebased = [Token(t.kind, t.text, t.start - offset) for t in body_tokens]
    return body, rebased


def has_top_level_order_by(tokens: list[Token]) -> bool:
    """True when ``ORDER BY`` appears outside any parentheses."""
    depth = 0
    sig = significant(tokens)
    for idx, tok in enumerate(sig):
        if tok.kind == "punct":
            if tok.text in "([":
                depth += 1
            elif tok.text in ")]":
                depth = max(0, depth - 1)
            continue
        if (
            depth == 0
            and tok.kind == "word"
            and tok.text.upper() == "ORDER"
            and idx + 1 < len(sig)
            and sig[idx + 1].kind == "word"
            and sig[idx + 1].text.upper() == "BY"
        ):
            return True
    return False
