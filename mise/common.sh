#!/usr/bin/env bash

#MISE hide=true

# 色付けヘルパー関数
# 基本色（ANSI 16色）
print_red() { printf '\e[31m%s\e[0m' "$*"; }
print_green() { printf '\e[32m%s\e[0m' "$*"; }
print_yellow() { printf '\e[33m%s\e[0m' "$*"; }
print_blue() { printf '\e[34m%s\e[0m' "$*"; }
print_magenta() { printf '\e[35m%s\e[0m' "$*"; }
print_cyan() { printf '\e[36m%s\e[0m' "$*"; }
# スタイル
print_dim() { printf '\e[2m%s\e[0m' "$*"; }
print_bold() { printf '\e[1m%s\e[0m' "$*"; }
# RGB カスタムカラー（引数: R G B テキスト）
print_rgb() {
  local r=$1 g=$2 b=$3
  shift 3
  printf '\e[38;2;%d;%d;%dm%s\e[0m' "$r" "$g" "$b" "$*"
}
# よく使うカスタムカラー
print_orange() { print_rgb 250 180 100 "$*"; }
print_soft_green() { print_rgb 150 255 200 "$*"; }
print_soft_blue() { print_rgb 160 190 255 "$*"; }
print_pink() { print_rgb 255 150 200 "$*"; }

# fzf が利用可能かを確認する。なければ案内を stderr に出して非ゼロで終了する。
require_fzf() {
  if ! command -v fzf >/dev/null 2>&1; then
    echo "fzf が見つかりません。サブコマンドを引数で指定してください。" >&2
    return 1
  fi
}

# Yes/No を fzf で選ばせる。選択されなければ非ゼロで終了（キャンセル扱い）。
# 使い方: if binary_choice "本当に実行しますか？"; then ...
binary_choice() {
  local res
  res=$(printf "Yes\nNo\n" | fzf --height 6 --border --prompt "${1:-選択}: ")
  if [ -z "$res" ]; then
    return 1
  fi
  echo "$res" | grep -iq "^yes$"
}

require_stdin_tty() {
  if [ ! -t 0 ]; then
    echo "Error: This function requires a TTY." >&2
    return 1
  fi
}

select_one_or_zero() {
  require_stdin_tty || return 1
  require_fzf || return 1

  local options=()
  local prompt="選択肢から1つを選択 > "
  local preview='printf "\033[38;5;214m%s\033[0m\n" {2..}'
  local preview_window='down,3,wrap'
  while [ $# -gt 0 ]; do
    case "$1" in
    --prompt | -p)
      prompt="$2"
      shift 2
      ;;
    --preview | -v)
      preview="$2"
      shift 2
      ;;
    --preview-window | -w)
      preview_window="$2"
      shift 2
      ;;
    --)
      shift
      options+=("$@")
      break
      ;;
    *)
      options+=("$1")
      shift
      ;;
    esac
  done

  if [ ${#options[@]} -eq 0 ]; then
    echo "選択肢が提供されていません" >&2
    return 1
  fi

  local selected
  selected=$(
    printf '%s\n' "${options[@]}" |
      fzf --prompt "$prompt" \
        --delimiter=$'\t' \
        --with-nth=1 \
        --color='prompt:75,pointer:211,marker:84,header:italic:245,hl:84,hl+:84:reverse' \
        --bind='tab:toggle+down,shift-tab:toggle+up,ctrl-/:toggle-preview' \
        --pointer='▶' \
        --marker='✓ ' \
        --header="$(
          printf '%b%s%b' '\033[34m' 'Tab ' '\033[0m'
          echo -n '選択切替 / '
          printf '%b%s%b' '\033[34m' 'Shift+Tab ' '\033[0m'
          echo -n '選択切替（逆へ移動）/ '
          printf '%b%s%b' '\033[34m' 'ESC ' '\033[0m'
          echo -n '選択なしで続行 / '
          printf '%b%s%b' '\033[34m' 'Ctrl+/ ' '\033[0m'
          echo -n 'preview 切替'
        )" \
        --preview "$preview" \
        --preview-window="$preview_window"
  ) || selected=""
  printf '%s\n' "$selected" | cut -d$'\t' -f1
}

select_one() {
  local selected
  selected=$(select_one_or_zero "$@") || return 1
  if [ -z "$selected" ]; then
    echo "選択されませんでした" >&2
    return 1
  fi
  printf '%s\n' "$selected"
}

select_multi_or_zero() {
  require_stdin_tty || return 1
  require_fzf || return 1

  local options=()
  local prompt="選択肢から複数を選択 > "
  local preview='printf "\033[38;5;214m%s\033[0m\n" {2..}'
  local preview_window='down,3,wrap'
  while [ $# -gt 0 ]; do
    case "$1" in
    --prompt | -p)
      prompt="$2"
      shift 2
      ;;
    --preview | -v)
      preview="$2"
      shift 2
      ;;
    --preview-window | -w)
      preview_window="$2"
      shift 2
      ;;
    --)
      shift
      options+=("$@")
      break
      ;;
    *)
      options+=("$1")
      shift
      ;;
    esac
  done

  if [ ${#options[@]} -eq 0 ]; then
    echo "選択肢が提供されていません" >&2
    return 1
  fi
  local selected
  selected=$(
    printf '%s\n' "${options[@]}" |
      fzf --multi \
        --prompt "$prompt" \
        --delimiter=$'\t' \
        --with-nth=1 \
        --color='prompt:75,pointer:211,marker:84,header:italic:245,hl:84,hl+:84:reverse' \
        --bind='tab:toggle+down,shift-tab:toggle+up,ctrl-a:toggle-all,ctrl-d:deselect-all,ctrl-/:toggle-preview' \
        --pointer='▶' \
        --marker='✓ ' \
        --header="$(
          printf '%b%s%b' '\033[34m' 'Tab ' '\033[0m'
          echo -n '選択切替 / '
          printf '%b%s%b' '\033[34m' 'Shift+Tab ' '\033[0m'
          echo -n '選択切替（逆へ移動）/ '
          printf '%b%s%b' '\033[34m' 'ESC ' '\033[0m'
          echo -n 'フラグなしで続行 / '
          printf '%b%s%b' '\033[34m' 'Ctrl+A ' '\033[0m'
          echo -n '全選択 / '
          printf '%b%s%b' '\033[34m' 'Ctrl+D ' '\033[0m'
          echo -n '全解除 / '
          printf '%b%s%b' '\033[34m' 'Ctrl+/ ' '\033[0m'
          echo -n 'preview 切替'
        )" \
        --preview "$preview" \
        --preview-window="$preview_window"
  ) || selected=""

  while IFS= read -r line; do
    [[ -n "$line" ]] || continue
    printf '%s\n' "${line%%$'\t'*}"
  done <<<"$selected"
}

select_multi() {
  local selected
  selected=$(select_multi_or_zero "$@") || return 1

  if [ -z "$selected" ]; then
    echo "選択されませんでした" >&2
    return 1
  fi

  printf '%s\n' "$selected"
}
