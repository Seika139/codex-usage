#!/usr/bin/env bash

#MISE description="定期実行するスケジューラの管理"
#MISE quiet=true
#USAGE arg "[subcommand]" help="実行するサブコマンド" {
#USAGE   choices "install" "status" "uninstall"
#USAGE }

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

# shellcheck source=mise/common.sh
# shellcheck disable=SC1091
source "${ROOT_DIR}/mise/common.sh"

SCHEDULE_SUBCOMMANDS=(
  $'install\t30分間隔で使用状況を記録するスケジュールを導入する'
  $'status\t定期実行するスケジューラの設定状況を表示する'
  $'uninstall\t使用状況を記録するスケジュールを削除する'
)

subcommand_or_fail() {
  local value="$1"
  case "$value" in
  install | status | uninstall)
    printf "%s\n" "$value"
    ;;
  "")
    if [ -t 0 ]; then
      select_one --prompt "サブコマンドを選択 > " -- "${SCHEDULE_SUBCOMMANDS[@]}"
      return
    fi
    print_red "[Error] サブコマンドを指定してください: install / status / uninstall"$'\n' >&2
    exit 1
    ;;
  *)
    print_red "[Error] Unknown Subcommand '$value'"$'\n' >&2
    exit 1
    ;;
  esac
}

subcommand="$(subcommand_or_fail "${usage_subcommand:-}")"

case "$subcommand" in
install)
  python -m codex_usage schedule install
  ;;
status)
  python -m codex_usage schedule status
  ;;
uninstall)
  python -m codex_usage schedule uninstall
  ;;
*)
  print_red "[Error] Unknown Subcommand '$subcommand'"$'\n' >&2
  exit 1
  ;;
esac
