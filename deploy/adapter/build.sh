#!/bin/bash
# Build the EMOSA adapter kit from this checkout:
#   deploy/adapter/build.sh [OUT_DIR]      (default dist/)
# -> OUT_DIR/emosa-adapter-VERSION.tar.gz: the adapter wheel, its pinned
#    requirements (with hashes), uv, install.sh, units and helper, the C lab
#    prototype's sources and the pod profiles, SHA256SUMS.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
OUT=$(realpath -m "${1:-$ROOT/dist}")
UV=${UV:-$(command -v uv || echo "$ROOT/.cache/opensync-lab-artifacts/uv")}
[ -x "$UV" ] || { echo "uv not found (set UV=)" >&2; exit 1; }
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml" | head -1)
KIT=$OUT/emosa-adapter-$VERSION
rm -rf "$KIT" && mkdir -p "$KIT/files"
"$UV" build -q --wheel --package emosa --out-dir "$KIT" --directory "$ROOT"
rm -f "$KIT/.gitignore"    # written by uv build; not part of the kit
"$UV" export -q --frozen --no-dev --no-emit-project --no-emit-workspace --directory "$ROOT" \
    --output-file "$KIT/requirements.txt"
install -m 0755 "$UV" "$KIT/uv"
install -m 0755 "$ROOT/deploy/adapter/install.sh" "$KIT/install.sh"
cp "$ROOT"/deploy/adapter/files/* "$KIT/files/"
mkdir -p "$KIT/c" "$KIT/profiles"
cp -r "$ROOT/c/CMakeLists.txt" "$ROOT/c/src" "$KIT/c/"
cp "$ROOT"/src/emosa/profiles/*.json "$KIT/profiles/"
cp "$ROOT/deploy/adapter/README.md" "$KIT/README.md"
echo "$VERSION $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)$(git -C "$ROOT" diff --quiet 2>/dev/null || echo +dirty)" > "$KIT/VERSION"
(cd "$KIT" && find . -type f ! -name SHA256SUMS -printf '%P\n' | sort | xargs sha256sum > SHA256SUMS)
tar -C "$OUT" -czf "$KIT.tar.gz" "$(basename "$KIT")"
echo "$KIT.tar.gz"
