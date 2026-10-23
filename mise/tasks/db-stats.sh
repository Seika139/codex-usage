#!/usr/bin/env bash

#MISE description="SQLiteデータベースのスナップショット件数とファイルサイズを表示する"
#MISE quiet=true
#USAGE flag "--db <path>" help="SQLite database path."

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

if [[ -n "${usage_db:-}" ]]; then
  exec python -m codex_usage db-stats --db "$usage_db"
fi

exec python -m codex_usage db-stats
