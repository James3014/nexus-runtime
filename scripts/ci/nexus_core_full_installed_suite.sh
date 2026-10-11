#!/usr/bin/env bash
set -euo pipefail

learning_sha="${1:?expected nexus-learning commit SHA}"
case "$learning_sha" in
  [0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f]) ;;
  *) echo "invalid nexus-learning SHA" >&2; exit 2 ;;
esac

repo_root="$(pwd -P)"
tmp_root="$(mktemp -d)"
cleanup() {
  rm -rf "$tmp_root"
}
trap cleanup EXIT

uv venv --python 3.11 --seed "$tmp_root/venv" >/dev/null
python_bin="$tmp_root/venv/bin/python"

"$python_bin" -m pip install --quiet   "pytest==9.0.2"   "pytest-asyncio==1.3.0"   "anyio==4.12.1"   "pydantic==2.13.5"   "PyYAML==6.0.3"   "nexus-learning @ git+https://github.com/James3014/nexus-learning.git@$learning_sha"

observed_learning="$("$python_bin" "$repo_root/scripts/ci/nexus_core_observe_learning_revision.py")"
test "$observed_learning" = "git-commit:$learning_sha"
printf 'observed_learning=%s\n' "$observed_learning"

"$python_bin" -m pip wheel --quiet --no-deps --wheel-dir "$tmp_root/wheel" "$repo_root"
"$python_bin" -m pip install --quiet --no-deps --force-reinstall "$tmp_root"/wheel/nexus_runtime-*.whl

cd "$tmp_root"
"$python_bin" -m pytest -q "$repo_root/tests"
