#!/usr/bin/env bash

#MISE description="lint"
#MISE quiet=true

# 途中でエラーが出ても他のファイルのリントは続行するため、set -e は使用しない
set -uo pipefail

# shellcheck disable=SC1091
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR" || exit 1

# shellcheck disable=SC1091
source "${ROOT_DIR}/mise/common.sh"

print_blue "linting Markdown, TOML, JSON with dprint"$'\n'
dprint check

python_path="$(mise which python)"

print_blue "Running Ruff lint"$'\n'
uv run --locked --python "$python_path" ruff check codex_usage

print_blue "Checking Ruff formatting"$'\n'
uv run --locked --python "$python_path" ruff format --check codex_usage

print_blue "Lint shell scripts with shfmt & shellcheck"$'\n'
shellcheck_files=()
while IFS= read -r -d '' file; do
  shellcheck_files+=("$file")
done < <(find . -type f \( -name "*.sh" -o -name "*.bash" \) -not -path "./.venv/*" -not -path "./mypy_cache/*" -not -path "./.git/*" -not -path "./.pytest_cache/*" -not -path "./ruff_cache/*" -print0)
if [ "${shellcheck_files[0]+_}" ]; then
  shfmt -d "${shellcheck_files[@]}"
  shellcheck -x -P SCRIPTDIR "${shellcheck_files[@]}"
else
  print_red "No shell scripts found; skipping shellcheck."$'\n'
fi

print_blue "linting YAML files with yamllint"$'\n'
yamllint .
