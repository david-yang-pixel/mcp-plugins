"""Catalog helpers shared by db_gateway_schema, timeout hints and scripts."""
import pytest

from db_gateway import schema_info


def test_catalog_sql_takes_table_names_as_bind_parameters():
    for sql in (schema_info.COLUMNS_SQL, schema_info.INDEXES_SQL):
        assert sql.count("%s") == 1 and "ANY(%s::text[])" in sql
        assert sql.lstrip().upper().startswith("SELECT")
    assert "information_schema.columns" in schema_info.COLUMNS_SQL
    assert "pg_indexes" in schema_info.INDEXES_SQL
    assert "%s" not in schema_info.ALL_INDEXES_SQL


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["workflow_runs"], [(None, "workflow_runs", False)]),
        ("apps, public.tenants", [(None, "apps", False), ("public", "tenants", False)]),
        (['"MixedCase"', "apps", "apps"], [(None, "MixedCase", True), (None, "apps", False)]),
    ],
)
def test_parse_table_refs(value, expected):
    assert schema_info.parse_table_refs(value) == expected


@pytest.mark.parametrize(
    "value",
    [None, 3, [], [""], [1], ["a b"], ["a;b c"], ["x" * 64], ["a.b.c"], [f"t{i}" for i in range(21)]],
)
def test_parse_table_refs_rejects(value):
    with pytest.raises(ValueError):
        schema_info.parse_table_refs(value)


def test_lookup_names_add_folded_case_for_unquoted_names():
    refs = schema_info.parse_table_refs(["Apps", '"Keep"'])
    assert schema_info.lookup_names(refs) == ["Apps", "apps", "Keep"]


def test_display_type():
    assert schema_info.display_type("character varying", "varchar", "255") == "varchar(255)"
    assert schema_info.display_type("timestamp with time zone", "timestamptz") == "timestamptz"
    assert schema_info.display_type("ARRAY", "_text") == "text[]"
    assert schema_info.display_type("USER-DEFINED", "my_enum") == "my_enum"
    assert schema_info.display_type("uuid", "uuid") == "uuid"


def test_build_schema_tables_matches_schema_and_reports_missing():
    refs = schema_info.parse_table_refs(["Workflow_Runs", "other.workflow_runs", "nope"])
    columns = [
        ["public", "workflow_runs", "id", "uuid", "uuid", None, "NO", None],
        ["audit", "workflow_runs", "id", "bigint", "int8", None, "YES", None],
    ]
    indexes = [["public", "workflow_runs", "workflow_runs_pkey", "CREATE UNIQUE INDEX ..."]]
    tables, missing = schema_info.build_schema_tables(refs, columns, indexes)
    # Unqualified: every schema, public first.
    assert [(t["schema"], t["table"]) for t in tables] == [
        ("public", "workflow_runs"), ("audit", "workflow_runs"),
    ]
    assert tables[0]["indexes"] == [{"name": "workflow_runs_pkey", "definition": "CREATE UNIQUE INDEX ..."}]
    assert tables[1]["columns"][0] == {"name": "id", "type": "bigint", "nullable": True}
    assert missing == ["other.workflow_runs", "nope"]


def test_indexes_for_tables():
    rows = [
        ["public", "apps", "apps_pkey", "CREATE UNIQUE INDEX apps_pkey ON public.apps USING btree (id)"],
        ["other", "apps", "apps_pkey", "CREATE UNIQUE INDEX apps_pkey ON other.apps USING btree (id)"],
    ]
    assert schema_info.indexes_for_tables([("other", "apps"), (None, "x")], rows) == [
        ("other.apps", ["CREATE UNIQUE INDEX apps_pkey ON other.apps USING btree (id)"]),
        ("x", []),
    ]
    labels = [label for label, _ in schema_info.indexes_for_tables([(None, "apps")], rows)]
    assert labels == ["public.apps", "other.apps"]


def test_dump_db_schema_script_reuses_shared_type_names(tmp_path):
    """scripts/dump_db_schema.py runs without third-party packages."""
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[2] / "scripts" / "dump_db_schema.py"
    (tmp_path / "langgenius.csv").write_text(
        "workflow_runs,id,uuid,PK,\nworkflow_runs,status,character varying,,\n", encoding="utf-8",
    )
    out = tmp_path / "schema.md"
    subprocess.run([sys.executable, "-S", str(script), str(tmp_path), str(out)], check=True, capture_output=True)
    assert "`status` varchar" in out.read_text(encoding="utf-8")
