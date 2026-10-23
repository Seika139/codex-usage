#!/usr/bin/env bash

#MISE description="定期実行するスケジューラの管理"
#MISE quiet=true
#USAGE arg "[subcommand]" help="実行するサブコマンド" {
#USAGE   choices "install" "status" "uninstall"
#USAGE }
#USAGE flag "--interval-minutes <minutes>" help="収集間隔（分、1〜44640。省略時は30）"

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

# shellcheck source=mise/common.sh
# shellcheck disable=SC1091
source "${ROOT_DIR}/mise/common.sh"

SCHEDULE_SUBCOMMANDS=(
  $'install\t使用状況を記録するスケジュールを導入する（既定は30分間隔）'
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
interval_minutes="${usage_interval_minutes:-}"

if [ -n "$interval_minutes" ] && [ "$subcommand" != "install" ]; then
  print_red "[Error] --interval-minutes は install でのみ指定できます"$'\n' >&2
  exit 1
fi

case "$subcommand" in
install)
  if [ -n "$interval_minutes" ]; then
    python -m codex_usage schedule install --interval-minutes "$interval_minutes"
  else
    python -m codex_usage schedule install
  fi
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
