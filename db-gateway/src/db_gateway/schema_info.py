"""Catalog (columns / indexes) lookups shared by the gateway and scripts.

Standard library only: ``scripts/dump_db_schema.py`` imports this module with
``src/`` on ``sys.path`` and no third-party packages installed.

All SQL here is read-only and takes table names as bind parameters (``%s``
placeholders for psycopg); no caller-provided text is ever spliced into SQL.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Sequence

# Abbreviations for common PostgreSQL type names (also used by dump_db_schema.py).
TYPE_ABBREV = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "double",
}

MAX_SCHEMA_TABLES = 20
_MAX_IDENTIFIER_LENGTH = 63
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")

# Columns of the requested tables (unqualified names: every non-system schema).
COLUMNS_SQL = """
SELECT c.table_schema::text, c.table_name::text, c.column_name::text,
       c.data_type::text, c.udt_name::text,
       c.character_maximum_length::text, c.is_nullable::text, c.column_default::text
FROM information_schema.columns c
WHERE c.table_schema NOT IN ('pg_catalog', 'information_schema')
  AND c.table_name = ANY(%s::text[])
ORDER BY c.table_schema, c.table_name, c.ordinal_position
""".strip()

# Indexes of the requested tables.
INDEXES_SQL = """
SELECT schemaname::text, tablename::text, indexname::text, indexdef::text
FROM pg_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
  AND tablename = ANY(%s::text[])
ORDER BY schemaname, tablename, indexname
""".strip()

# Every user index of the database (cached for timeout hints).
ALL_INDEXES_SQL = """
SELECT schemaname::text, tablename::text, indexname::text, indexdef::text
FROM pg_indexes
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
ORDER BY schemaname, tablename, indexname
""".strip()


def shorten_type(t: str) -> str:
    return TYPE_ABBREV.get(t, t)


def display_type(data_type: str | None, udt_name: str | None, max_length: str | None = None) -> str:
    """Readable column type from information_schema.columns fields."""
    data_type = data_type or ""
    udt_name = udt_name or ""
    if data_type == "ARRAY":
        base = udt_name[1:] if udt_name.startswith("_") else udt_name
        return f"{shorten_type(base)}[]"
    if data_type == "USER-DEFINED":
        return udt_name or data_type
    text = shorten_type(data_type)
    if max_length and str(max_length).isdigit() and data_type in ("character varying", "character"):
        text = f"{text}({max_length})"
    return text


_NAME_PART = r'(?:"(?:[^"]|"")+"|[^".\s]+)'
_TABLE_REF_RE = re.compile(rf"^({_NAME_PART})(?:\.({_NAME_PART}))?$")


def _unquote(part: str) -> tuple[str, bool]:
    if part.startswith('"') and part.endswith('"') and len(part) >= 2:
        return part[1:-1].replace('""', '"'), True
    return part, False


def parse_table_refs(value: Any) -> list[tuple[str | None, str, bool]]:
    """Validate the ``tables`` argument: ``"t"``, ``"schema.t"`` or ``'"Quoted"'``.

    Accepts a list of strings or one string (comma-separated allowed). Returns
    ``(schema, table, quoted)``; ``quoted`` means the name must match exactly.
    Raises ValueError with a message for the caller.
    """
    if isinstance(value, str):
        items: list[Any] = [v for v in value.split(",")]
    elif isinstance(value, (list, tuple)):
        items = list(value)
    else:
        raise ValueError("tables 必须是表名字符串数组，例如 [\"workflow_runs\"]")
    refs: list[tuple[str | None, str, bool]] = []
    for item in items:
        if not isinstance(item, str):
            raise ValueError("tables 中的每一项都必须是字符串")
        text = item.strip()
        if not text:
            continue
        m = _TABLE_REF_RE.match(text)
        if not m:
            raise ValueError(f"无效的表名: {text[:80]!r}（格式：table 或 schema.table）")
        if m.group(2) is None:
            schema, (table, quoted) = None, _unquote(m.group(1))
        else:
            schema, _ = _unquote(m.group(1))
            table, quoted = _unquote(m.group(2))
        for part in (schema, table):
            if part is not None and len(part) > _MAX_IDENTIFIER_LENGTH:
                raise ValueError(f"表名过长（超过 {_MAX_IDENTIFIER_LENGTH} 字符）: {text[:80]!r}")
        ref = (schema, table, quoted)
        if ref not in refs:
            refs.append(ref)
    if not refs:
        raise ValueError("tables 不能为空，请至少提供一个表名")
    if len(refs) > MAX_SCHEMA_TABLES:
        raise ValueError(f"一次最多查询 {MAX_SCHEMA_TABLES} 张表，收到 {len(refs)} 张")
    return refs


def lookup_names(refs: Iterable[tuple[str | None, str, bool]]) -> list[str]:
    """Table names to bind into COLUMNS_SQL / INDEXES_SQL (exact + folded case)."""
    names: list[str] = []
    for _schema, table, quoted in refs:
        for name in (table,) if quoted else (table, table.lower()):
            if name not in names:
                names.append(name)
    return names


def _matches(ref: tuple[str | None, str, bool], schema: str, table: str) -> bool:
    ref_schema, ref_table, quoted = ref
    if ref_schema is not None and schema not in (ref_schema, ref_schema.lower()):
        return False
    return table == ref_table or (not quoted and table == ref_table.lower())


def build_schema_tables(
    refs: Sequence[tuple[str | None, str, bool]],
    column_rows: Sequence[Sequence[Any]],
    index_rows: Sequence[Sequence[Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Group catalog rows per table. Returns (tables, names not found)."""
    tables: dict[tuple[str, str], dict[str, Any]] = {}
    for row in column_rows:
        schema, table, column, data_type, udt_name, max_len, nullable, default = (
            list(row) + [None] * 8
        )[:8]
        entry = tables.setdefault(
            (schema, table), {"schema": schema, "table": table, "columns": [], "indexes": []},
        )
        col: dict[str, Any] = {
            "name": column,
            "type": display_type(data_type, udt_name, max_len),
            "nullable": str(nullable).upper() == "YES",
        }
        if default is not None:
            col["default"] = default
        entry["columns"].append(col)
    for row in index_rows:
        schema, table, index_name, index_def = (list(row) + [None] * 4)[:4]
        entry = tables.setdefault(
            (schema, table), {"schema": schema, "table": table, "columns": [], "indexes": []},
        )
        entry["indexes"].append({"name": index_name, "definition": index_def})

    result: list[dict[str, Any]] = []
    missing: list[str] = []
    for ref in refs:
        matched = [
            entry for (schema, table), entry in tables.items()
            if _matches(ref, str(schema), str(table))
        ]
        exact = [e for e in matched if e["table"] == ref[1]]
        matched = exact or matched
        if not matched:
            missing.append(f"{ref[0]}.{ref[1]}" if ref[0] else ref[1])
        for entry in sorted(matched, key=lambda e: (e["schema"] != "public", e["schema"])):
            if entry not in result:
                result.append(entry)
    return result, missing


def indexes_for_tables(
    refs: Sequence[tuple[str | None, str]],
    index_rows: Sequence[Sequence[Any]],
) -> list[tuple[str, list[str]]]:
    """``[(label, [indexdef, ...])]`` for tables referenced by a query.

    ``refs`` come from ``sql_text.referenced_tables`` (already case-folded).
    Tables without any match in ``index_rows`` are reported with an empty list.
    """
    out: list[tuple[str, list[str]]] = []
    for schema, table in refs:
        by_table: dict[tuple[str, str], list[str]] = {}
        for row in index_rows:
            r_schema, r_table, _name, index_def = (list(row) + [None] * 4)[:4]
            if r_table != table or (schema is not None and r_schema != schema):
                continue
            by_table.setdefault((str(r_schema), str(r_table)), []).append(str(index_def))
        if not by_table:
            out.append((f"{schema}.{table}" if schema else table, []))
            continue
        for (r_schema, r_table), defs in sorted(by_table.items(), key=lambda kv: kv[0][0] != "public"):
            out.append((f"{r_schema}.{r_table}", defs))
    return out
