"""Read-only SQL validator: accepted / rejected cases and error messages."""
from __future__ import annotations

import pytest

from db_gateway.postgres import (
    PsqlSafetyError,
    sql_has_top_level_order_by,
    validate_read_only_sql,
    wrap_query,
)


@pytest.mark.parametrize(
    ("sql", "body"),
    [
        # previously accepted, must not regress
        ("SELECT 1", "SELECT 1"),
        ("select id, name from tenants limit 1", "select id, name from tenants limit 1"),
        ("SELECT 1;", "SELECT 1"),
        ("  SELECT 1 ;  ;\n", "SELECT 1"),
        ("WITH t AS (SELECT 1 AS x) SELECT * FROM t", "WITH t AS (SELECT 1 AS x) SELECT * FROM t"),
        ("-- leading comment\nSELECT 1", "SELECT 1"),
        ("/* block */ SELECT 1", "SELECT 1"),
        # newly accepted: leading parentheses
        ("(SELECT 1) UNION ALL (SELECT 2)", "(SELECT 1) UNION ALL (SELECT 2)"),
        ("((SELECT 1))", "((SELECT 1))"),
        ("(WITH t AS (SELECT 1) SELECT * FROM t)", "(WITH t AS (SELECT 1) SELECT * FROM t)"),
        # trailing comments / semicolons are stripped so wrapping stays valid
        ("SELECT 1 -- trailing comment", "SELECT 1"),
        ("SELECT 1; -- done", "SELECT 1"),
        ("SELECT 1 /* c */ ;", "SELECT 1"),
        # semicolons / comment markers inside literals are not statement separators
        ("SELECT 'a;b' AS v", "SELECT 'a;b' AS v"),
        ("SELECT '--x' AS v, 2", "SELECT '--x' AS v, 2"),
        ("SELECT $$;$$ AS v", "SELECT $$;$$ AS v"),
        ("SELECT $tag$ a;b $tag$ AS v", "SELECT $tag$ a;b $tag$ AS v"),
        ("SELECT E'it\\'s;' AS v", "SELECT E'it\\'s;' AS v"),
        ('SELECT 1 AS "semi;colon"', 'SELECT 1 AS "semi;colon"'),
        ("SELECT data->>'k' FROM t WHERE x::text = '1'", "SELECT data->>'k' FROM t WHERE x::text = '1'"),
        ("SELECT * FROM t WHERE id = $1", "SELECT * FROM t WHERE id = $1"),
    ],
)
def test_accepts_read_only_queries(sql, body):
    assert validate_read_only_sql(sql) == body


@pytest.mark.parametrize(
    ("sql", "fragment"),
    [
        ("DELETE FROM tenants WHERE id=1", "DELETE"),
        ("UPDATE tenants SET name='x'", "UPDATE"),
        ("(DELETE FROM t)", "DELETE"),
        ("WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", "DELETE"),
        ("SELECT 1; SELECT 2", "多条语句"),
        ("SELECT 1; DROP TABLE t", "多条语句"),
        ("SELECT '/*' ; DELETE FROM t; SELECT '*/'", "多条语句"),
        ("SHOW search_path", "SHOW"),
        ("SET statement_timeout = 0", "SET"),
        ("COPY t TO STDOUT", "COPY"),
        ("EXPLAIN SELECT 1", "EXPLAIN"),
        ("VALUES (1)", "VALUES"),
        ("TABLE tenants", "TABLE"),
        ("* FROM t", "*"),
        ("SELECT * FROM t WHERE action = 'UPDATE'", "字符串常量"),
    ],
)
def test_rejects_with_accurate_reason(sql, fragment):
    with pytest.raises(PsqlSafetyError) as exc:
        validate_read_only_sql(sql)
    assert fragment in str(exc.value)


def test_paren_union_is_not_reported_as_empty():
    # regression: used to fail with "empty SQL"
    assert validate_read_only_sql("(SELECT 1) UNION ALL (SELECT 2)")


@pytest.mark.parametrize("sql", ["", "   ", ";", " ; ; ", "-- only a comment", "/* x */"])
def test_rejects_empty(sql):
    with pytest.raises(PsqlSafetyError, match="为空"):
        validate_read_only_sql(sql)


def test_rejects_only_parentheses():
    with pytest.raises(PsqlSafetyError, match="括号"):
        validate_read_only_sql("(( ))")


@pytest.mark.parametrize(
    "sql",
    ["SELECT 'abc", 'SELECT "abc', "SELECT 1 /* never closed", "SELECT $x$ body"],
)
def test_rejects_unterminated_tokens(sql):
    with pytest.raises(PsqlSafetyError, match="未闭合"):
        validate_read_only_sql(sql)


def test_wrapped_query_still_validates_and_survives_trailing_comment():
    body = validate_read_only_sql("SELECT 1 AS a -- note\n;")
    wrapped = wrap_query(body)
    assert wrapped.startswith("SELECT * FROM (\nSELECT 1 AS a\n)")
    assert validate_read_only_sql(wrapped) == wrapped


def test_top_level_order_by_detection():
    assert sql_has_top_level_order_by("SELECT * FROM t ORDER BY id")
    assert sql_has_top_level_order_by("(SELECT 1) UNION ALL (SELECT 2) order  by 1")
    assert not sql_has_top_level_order_by("SELECT * FROM (SELECT * FROM t ORDER BY id) s")
    assert not sql_has_top_level_order_by("SELECT 'ORDER BY' AS x")
    assert not sql_has_top_level_order_by("SELECT 1 -- ORDER BY 1")


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT pg_advisory_lock(1)",
        "select pg_catalog.pg_try_advisory_lock_shared(1, 2)",
        'SELECT "pg_advisory_lock"(1)',
        "SELECT pg_advisory_xact_lock(1)",
        "SELECT * FROM t WHERE PG_ADVISORY_UNLOCK_ALL() IS NOT NULL",
    ],
)
def test_advisory_lock_functions_are_rejected(sql):
    # Session-level advisory locks survive ROLLBACK and would leak to the next
    # caller of a pooled connection.
    with pytest.raises(PsqlSafetyError, match="advisory lock"):
        validate_read_only_sql(sql)


def test_advisory_lock_name_in_a_string_literal_is_allowed():
    assert validate_read_only_sql("SELECT 'pg_advisory_lock' AS name") == "SELECT 'pg_advisory_lock' AS name"


def test_unicode_quoted_advisory_lock_is_rejected():
    with pytest.raises(PsqlSafetyError, match="advisory lock"):
        validate_read_only_sql('SELECT U&"pg_advisory_lock"(1)')


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM workflow_runs WHERE id = 1", [(None, "workflow_runs")]),
        (
            'SELECT a.id FROM public.apps a JOIN "Tenants" t ON t.id = a.tenant_id LEFT JOIN accounts ON true',
            [("public", "apps"), (None, "Tenants"), (None, "accounts")],
        ),
        (
            "WITH x AS (SELECT * FROM messages), y(c) AS MATERIALIZED (SELECT 1 FROM conversations) "
            "SELECT * FROM x, y, apps z, tenants",
            [(None, "messages"), (None, "conversations"), (None, "apps"), (None, "tenants")],
        ),
        (
            "SELECT extract(year FROM created_at), coalesce((SELECT 1 FROM t2), 0) FROM T1",
            [(None, "t2"), (None, "t1")],
        ),
        (
            "SELECT * FROM generate_series(1, 3) g, (SELECT 1 FROM sub) s "
            "CROSS JOIN LATERAL (SELECT 1 FROM lat) l",
            [(None, "sub"), (None, "lat")],
        ),
        ("SELECT id FROM t WHERE id IN (SELECT tid FROM u) ORDER BY id", [(None, "t"), (None, "u")]),
        ("(SELECT 1 FROM a) UNION ALL (SELECT 2 FROM b)", [(None, "a"), (None, "b")]),
        ("SELECT trim(both ' ' FROM name) FROM ONLY accounts AS acc", [(None, "accounts")]),
        ("SELECT 'FROM fake' FROM real_table -- FROM comment", [(None, "real_table")]),
        ("SELECT 1", []),
    ],
)
def test_referenced_tables(sql, expected):
    from db_gateway.sql_text import referenced_tables, tokenize

    assert referenced_tables(tokenize(sql)) == expected
