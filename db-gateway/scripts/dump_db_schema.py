"""把 3 份 schema CSV 合成一份 markdown SCHEMA。

Input:  CSV files at <tmp_dir>/{langgenius,billing,dify_plugin_daemon}.csv
        每行：table_name, column_name, data_type, is_pk, fk_target

Output: <out_path> 一份 markdown，每张表一行紧凑 column 列表。

CLI: python3 dump_db_schema.py <tmp_dir> <out_path>
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

DB_FILES = [
    ("langgenius", "Dify SaaS 主业务库（tenant / app / workflow / dataset / 用户数据）"),
    ("billing", "订阅 / 订单 / 退款 / 配额计费"),
    ("dify_plugin_daemon", "插件运行守护（plugin install / runtime / endpoint）"),
]

# 这些列名是常见样板字段，schema 不用列出
NOISE_COLUMNS = {"created_at", "updated_at", "deleted_at"}

# 简化常见 PG 类型缩写
TYPE_ABBREV = {
    "character varying": "varchar",
    "timestamp without time zone": "timestamp",
    "timestamp with time zone": "timestamptz",
    "double precision": "double",
}


def shorten_type(t: str) -> str:
    return TYPE_ABBREV.get(t, t)


def render(tmp_dir: Path, out_path: Path) -> None:
    out: list[str] = []
    out.append("# Dify SaaS Database Schema")
    out.append("")
    out.append(
        "本文件由 `scripts/dump_db_schema.sh` 自动生成（直接 introspect 生产库），"
        "供 agent 写 SQL 时参考真实表/列结构，避免凭记忆瞎猜。"
    )
    out.append("")
    out.append(
        "**说明**：每张表只列业务列，省略 `created_at` / `updated_at` / "
        "`deleted_at` 等样板时间戳。PK 标 `*`，FK 后跟 `→ref_table.col`。"
    )
    out.append("")

    for db_name, db_desc in DB_FILES:
        p = tmp_dir / f"{db_name}.csv"
        if not p.exists():
            continue
        tables: dict[str, list[tuple[str, str, str, str]]] = defaultdict(list)
        with p.open() as f:
            for row in csv.reader(f):
                if len(row) < 5:
                    continue
                table, col, dtype, is_pk, fk = row[:5]
                if col in NOISE_COLUMNS and not is_pk and not fk:
                    continue
                tables[table].append((col, shorten_type(dtype), is_pk, fk))

        # sql_query 入参 database= 的字面值
        db_arg = "prod" if db_name == "langgenius" else db_name.split("_")[0]
        out.append(f"## {db_name}")
        out.append("")
        out.append(f"**用途**：{db_desc}")
        out.append(f"**调用**：`sql_query database={db_arg} sql=\"...\"`")
        out.append(f"**表数**：{len(tables)}")
        out.append("")
        for table in sorted(tables.keys()):
            cols = tables[table]
            out.append(f"### `{table}`")
            inline = []
            for col, dtype, is_pk, fk in cols:
                tag = ""
                if is_pk:
                    tag += "*"
                if fk:
                    tag += f" →{fk}"
                inline.append(f"`{col}` {dtype}{tag}")
            out.append(", ".join(inline))
            out.append("")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out), encoding="utf-8")
    size = out_path.stat().st_size
    print(f"Wrote {out_path}: {size} bytes (~{size // 4} tokens)")


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"Usage: {argv[0]} <tmp_dir_with_csvs> <out_md_path>", file=sys.stderr)
        return 2
    tmp_dir = Path(argv[1])
    out_path = Path(argv[2])
    if not tmp_dir.exists():
        print(f"tmp dir does not exist: {tmp_dir}", file=sys.stderr)
        return 1
    render(tmp_dir, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
