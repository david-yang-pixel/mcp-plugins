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


# Words that may directly precede a "(" without making it a function call.
_GROUP_PREFIX_WORDS = {
    "FROM", "JOIN", "ON", "AND", "OR", "NOT", "WHERE", "IN", "EXISTS", "AS",
    "LATERAL", "UNION", "INTERSECT", "EXCEPT", "ALL", "SELECT", "WITH", "HAVING",
    "BY", "THEN", "ELSE", "WHEN", "DISTINCT", "USING", "MATERIALIZED", "RECURSIVE",
}
_QUERY_START_WORDS = {"SELECT", "WITH", "VALUES", "TABLE"}
# Words that end a FROM item (so they are not taken as its alias).
_FROM_ITEM_STOP_WORDS = {
    "WHERE", "GROUP", "HAVING", "ORDER", "LIMIT", "OFFSET", "FETCH", "FOR", "WINDOW",
    "UNION", "INTERSECT", "EXCEPT", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
    "NATURAL", "ON", "USING", "TABLESAMPLE", "RETURNING", "SELECT", "FROM", "WITH",
}


def _identifier(tok: Token) -> str | None:
    """Catalog name of an identifier token (unquoted names fold to lower case)."""
    if tok.kind == "word" and _WORD_START.match(tok.text):
        return tok.text.lower()
    if tok.kind == "qident":
        return re.sub(r'^(?:[Uu]&)?"|"$', "", tok.text).replace('""', '"')
    return None


def referenced_tables(tokens: list[Token]) -> list[tuple[str | None, str]]:
    """Best-effort ``(schema, table)`` list of relations named after FROM/JOIN.

    Used only to add index hints to timeout errors, so it never raises and may
    miss exotic syntax. CTE names and ``FROM`` inside function calls such as
    ``EXTRACT(year FROM ts)`` are skipped.
    """
    sig = significant(tokens)
    n = len(sig)
    # Is each "(" a function-call frame?  FROM inside a call frame is not a FROM clause.
    in_call: list[bool] = []
    inside_call: list[bool] = []  # is the innermost open frame at token i a call?
    for idx, tok in enumerate(sig):
        inside_call.append(bool(in_call) and in_call[-1])
        if tok.kind != "punct":
            continue
        if tok.text == "(":
            prev = sig[idx - 1] if idx > 0 else None
            nxt = sig[idx + 1] if idx + 1 < n else None
            starts_query = (
                nxt is not None and nxt.kind == "word" and nxt.text.upper() in _QUERY_START_WORDS
            )
            after_name = (
                prev is not None
                and prev.kind in ("word", "qident")
                and prev.text.upper() not in _GROUP_PREFIX_WORDS
            )
            in_call.append(after_name and not starts_query)
        elif tok.text == ")" and in_call:
            in_call.pop()

    cte_names: set[str] = set()
    for idx, tok in enumerate(sig):
        if not (tok.kind == "word" and tok.text.upper() == "AS"):
            continue
        j = idx + 1
        if j < n and sig[j].kind == "word" and sig[j].text.upper() == "NOT":
            j += 1
        if j < n and sig[j].kind == "word" and sig[j].text.upper() == "MATERIALIZED":
            j += 1
        if not (j < n and sig[j].kind == "punct" and sig[j].text == "("):
            continue
        k = idx - 1
        if k >= 0 and sig[k].kind == "punct" and sig[k].text == ")":
            depth = 0
            while k >= 0:  # skip the CTE column list
                if sig[k].kind == "punct" and sig[k].text == ")":
                    depth += 1
                elif sig[k].kind == "punct" and sig[k].text == "(":
                    depth -= 1
                    if depth == 0:
                        break
                k -= 1
            k -= 1
        if k >= 0:
            name = _identifier(sig[k])
            if name:
                cte_names.add(name)

    def skip_parens(i: int) -> int:
        depth = 0
        while i < n:
            if sig[i].kind == "punct" and sig[i].text == "(":
                depth += 1
            elif sig[i].kind == "punct" and sig[i].text == ")":
                depth -= 1
                if depth <= 0:
                    return i + 1
            i += 1
        return i

    found: list[tuple[str | None, str]] = []

    def parse_item(i: int) -> int:
        while i < n and sig[i].kind == "word" and sig[i].text.upper() in ("ONLY", "LATERAL"):
            i += 1
        if i >= n:
            return i
        if sig[i].kind == "punct" and sig[i].text == "(":
            return skip_parens(i)
        first = _identifier(sig[i])
        if first is None or (sig[i].kind == "word" and sig[i].text.upper() in _FROM_ITEM_STOP_WORDS):
            return i
        schema: str | None = None
        name = first
        i += 1
        if i + 1 < n and sig[i].kind == "punct" and sig[i].text == ".":
            second = _identifier(sig[i + 1])
            if second is not None:
                schema, name = first, second
                i += 2
        if i < n and sig[i].kind == "punct" and sig[i].text == "(":
            return skip_parens(i)  # set-returning function, not a table
        if schema is not None or name not in cte_names:
            if (schema, name) not in found:
                found.append((schema, name))
        return i

    def skip_alias(i: int) -> int:
        if i < n and sig[i].kind == "word" and sig[i].text.upper() == "AS":
            i += 1
        if (
            i < n
            and sig[i].kind in ("word", "qident")
            and _identifier(sig[i]) is not None
            and sig[i].text.upper() not in _FROM_ITEM_STOP_WORDS
        ):
            i += 1
            if i < n and sig[i].kind == "punct" and sig[i].text == "(":
                i = skip_parens(i)  # column alias list
        return i

    for idx, tok in enumerate(sig):
        if tok.kind != "word" or inside_call[idx]:
            continue
        keyword = tok.text.upper()
        if keyword == "JOIN":
            parse_item(idx + 1)
        elif keyword == "FROM":
            i = parse_item(idx + 1)
            i = skip_alias(i)
            while i < n and sig[i].kind == "punct" and sig[i].text == ",":
                i = skip_alias(parse_item(i + 1))
    return found
