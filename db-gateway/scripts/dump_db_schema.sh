#!/usr/bin/env bash
# Refresh DB schema cheat sheet by introspecting prod DBs via tsh-proxy-db.
#
# Reads from local Teleport proxy ports (started by systemd):
#   prod:    127.0.0.1:56061  (langgenius)
#   billing: 127.0.0.1:56062  (billing)
#   plugin:  127.0.0.1:56063  (dify_plugin_daemon)
#
# Output:
#   /opt/zd-agent/resources/saas版知识库/database_schema.md  (loaded into agent
#   system prompt; falls back to "no schema" if missing)
#
# Run on:  the production server (<agent-server-ip>).
# Cadence: weekly via cron, or on-demand after schema migrations.
set -euo pipefail

OUT_DIR="${ZDA_KB_DIR:-/opt/zd-agent/resources/saas版知识库}"
OUT_PATH="$OUT_DIR/database_schema.md"
TMP=$(mktemp -d)
trap "rm -rf $TMP" EXIT

# Schema introspection SQL: dump (table, col, type, is_pk, fk_target) for every
# user-table column. Used by dump_db_schema.py to render compact markdown.
cat > "$TMP/dump.sql" <<'SQL'
WITH cols AS (
  SELECT c.table_name, c.ordinal_position, c.column_name, c.data_type
  FROM information_schema.columns c
  WHERE c.table_schema = 'public'
),
pks AS (
  SELECT tc.table_name, kcu.column_name
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
   AND tc.table_schema = kcu.table_schema
  WHERE tc.constraint_type = 'PRIMARY KEY'
    AND tc.table_schema = 'public'
),
fks AS (
  SELECT tc.table_name, kcu.column_name AS local_col,
         ccu.table_name AS ref_table, ccu.column_name AS ref_col
  FROM information_schema.table_constraints tc
  JOIN information_schema.key_column_usage kcu
    ON tc.constraint_name = kcu.constraint_name
   AND tc.table_schema = kcu.table_schema
  JOIN information_schema.constraint_column_usage ccu
    ON tc.constraint_name = ccu.constraint_name
   AND tc.table_schema = ccu.table_schema
  WHERE tc.constraint_type = 'FOREIGN KEY'
    AND tc.table_schema = 'public'
)
SELECT
  c.table_name,
  c.column_name,
  c.data_type,
  CASE WHEN p.column_name IS NOT NULL THEN 'PK' ELSE '' END AS is_pk,
  COALESCE(
    (SELECT ref_table || '.' || ref_col FROM fks
     WHERE table_name = c.table_name AND local_col = c.column_name LIMIT 1),
    ''
  ) AS fk
FROM cols c
LEFT JOIN pks p
  ON p.table_name = c.table_name AND p.column_name = c.column_name
ORDER BY c.table_name, c.ordinal_position;
SQL

# psql lives inside the db-gateway container (--network=host so 127.0.0.1 works).
docker cp "$TMP/dump.sql" db-gateway:/tmp/dump.sql

for entry in "langgenius:56061" "billing:56062" "dify_plugin_daemon:56063"; do
  DB="${entry%%:*}"
  PORT="${entry##*:}"
  echo "introspecting $DB on $PORT ..."
  docker exec db-gateway psql \
    "host=127.0.0.1 port=$PORT user=pro_ops_teleport_read dbname=$DB sslmode=disable" \
    --csv -t -f /tmp/dump.sql > "$TMP/$DB.csv"
done

python3 "$(dirname "$0")/dump_db_schema.py" "$TMP" "$OUT_PATH"
echo "Schema refreshed at $OUT_PATH"
