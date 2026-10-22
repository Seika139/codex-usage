#!/usr/bin/env bash

#MISE description="format"
#MISE quiet=true

# 途中でエラーが出ても他のファイルのフォーマットは続行するため、set -e は使用しない
set -uo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

# shellcheck disable=SC1091
source "${ROOT_DIR}/mise/common.sh"

print_blue "formatting Markdown, TOML, JSON with dprint"$'\n'
dprint fmt

python_path="$(mise which python)"

print_blue "Applying Ruff lint fixes"$'\n'
uv run --locked --python "$python_path" ruff check --fix codex_usage

print_blue "Formatting Python code"$'\n'
uv run --locked --python "$python_path" ruff format codex_usage

print_blue "formatting shell scripts with shfmt"$'\n'
shell_files=()
while IFS= read -r -d '' file; do
  shell_files+=("$file")
done < <(find . -type f \( -name "*.sh" -o -name "*.bash" \) -not -path "./.venv/*" -not -path "./mypy_cache/*" -not -path "./.git/*" -not -path "./.pytest_cache/*" -not -path "./ruff_cache/*" -print0)
if [ "${shell_files[0]+_}" ]; then
  find "${shell_files[@]}" -type f \
    \( -name "*.sh" -o -name "*.bash" \) -print0 |
    xargs -0 shfmt -w
else
  print_red "No shell scripts found; skipping shfmt."$'\n'
fi

# yamllint には自動修正機能が無いため、parsable 出力を解析して
# `trailing-spaces` 違反の行だけ sed で行末スペースを除去する。
# line-length / indentation など他の違反は dot-lint 側で検出・手動修正する。
print_blue "formatting YAML files with yamllint + sed"$'\n'
yamllint -f parsable . | while IFS= read -r finding; do
  # 形式: file:line:col: [level] message (rule-name)
  if [[ "$finding" =~ ^([^:]+):([0-9]+):[0-9]+:\ .*\(trailing-spaces\)$ ]]; then
    file="${BASH_REMATCH[1]}"
    line_num="${BASH_REMATCH[2]}"
    # `-i.bak` は BSD/GNU 双方で in-place 編集として動く portable 形式。
    # `-E` (ERE) で正規表現方言差を回避する。
    if sed -i.bak -E "${line_num}s/[[:space:]]+\$//" "${file}"; then
      rm -f "${file}.bak"
      print_green "Fixed trailing spaces: ${file}:${line_num}"$'\n'
    else
      print_red "Failed to fix trailing spaces: ${file}:${line_num}"$'\n'
    fi
  fi
done
