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

find_ruff() {
  local windows_ruff="$ROOT_DIR/.venv/Scripts/ruff.exe"
  local posix_ruff="$ROOT_DIR/.venv/bin/ruff"
  if [ -f "$windows_ruff" ]; then
    printf '%s\n' "$windows_ruff"
  elif [ -x "$posix_ruff" ]; then
    printf '%s\n' "$posix_ruff"
  else
    return 1
  fi
}

print_blue "linting Markdown, TOML, JSON with dprint"$'\n'
dprint check

python_path="$(mise which python)"
ruff_ready=true
if ! uv lock --check --python "$python_path"; then
  print_red "uv.lock check failed; skipping Ruff checks."$'\n'
  ruff_ready=false
fi
if [ "$ruff_ready" = true ]; then
  locked_ruff_version="$("$python_path" -c 'import tomllib; from pathlib import Path; print(next(package["version"] for package in tomllib.loads(Path("uv.lock").read_text())["package"] if package["name"] == "ruff"))')"
  ruff_path="$(find_ruff || true)"
  installed_ruff_version=""
  if [ -n "$ruff_path" ]; then
    installed_ruff_version="$("$ruff_path" --version 2>/dev/null || true)"
  fi
  if [ "$installed_ruff_version" != "ruff $locked_ruff_version" ]; then
    # Sync only when Ruff is missing or stale; the LSP may be using this environment.
    if ! uv run --locked --python "$python_path" ruff --version >/dev/null; then
      print_red "Could not prepare locked Ruff; skipping Ruff checks."$'\n'
      ruff_ready=false
    else
      ruff_path="$(find_ruff || true)"
      installed_ruff_version=""
      if [ -n "$ruff_path" ]; then
        installed_ruff_version="$("$ruff_path" --version 2>/dev/null || true)"
      fi
      if [ "$installed_ruff_version" != "ruff $locked_ruff_version" ]; then
        print_red "Could not prepare locked Ruff; skipping Ruff checks."$'\n'
        ruff_ready=false
      fi
    fi
  fi
fi

if [ "$ruff_ready" = true ]; then
  print_blue "Running Ruff lint"$'\n'
  "$ruff_path" check codex_usage

  print_blue "Checking Ruff formatting"$'\n'
  "$ruff_path" format --check codex_usage
fi

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
lint_status=$?

if [ "$ruff_ready" = false ]; then
  exit 1
fi
exit "$lint_status"
