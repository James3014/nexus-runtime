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

donor_sha=471a281badda342ccab26606e0c46cbca6867cbb
donor_repo="$tmp_root/nexus-new-donor.git"
donor_stage="$tmp_root/nexus-new-donor-export"
donor_locator=/private/tmp/astra-production-integrated-20260909

test ! -e "$donor_locator"
git init --bare -q "$donor_repo"
git -C "$donor_repo" fetch --quiet --no-tags --depth=1   https://github.com/James3014/Nexus-new.git "$donor_sha"
test "$(git --git-dir="$donor_repo" rev-parse "$donor_sha^{commit}")" = "$donor_sha"
donor_tree="$(git --git-dir="$donor_repo" rev-parse "$donor_sha^{tree}")"
mkdir -p "$donor_stage"
git --git-dir="$donor_repo" archive "$donor_sha" | tar -x -C "$donor_stage"
sudo mkdir -p "$donor_locator"
sudo cp -a "$donor_stage/." "$donor_locator/"
sudo git -C "$donor_locator" init -q
sudo mkdir -p "$donor_locator/.git/objects/info"
printf '%s\n' "$donor_repo/objects" | sudo tee "$donor_locator/.git/objects/info/alternates" >/dev/null
sudo git -C "$donor_locator" update-ref refs/heads/frozen-donor "$donor_sha"
sudo git -C "$donor_locator" symbolic-ref HEAD refs/heads/frozen-donor
sudo git -C "$donor_locator" read-tree "$donor_sha"
test "$(sudo git -C "$donor_locator" rev-parse HEAD)" = "$donor_sha"
test "$(sudo git -C "$donor_locator" rev-parse HEAD^{tree})" = "$donor_tree"

cd "$tmp_root"
"$python_bin" -m pytest -q "$repo_root/tests"
