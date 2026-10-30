#!/usr/bin/env bash
# Build a Decky-installable plugin zip from the current working tree.
#
# Mirrors the file list of the CI release artifact (DeckyTranslatorRu.zip):
#   decky-translator-ru/
#     dist/  main.py  py_modules/providers/  package.json  plugin.json  LICENSE  README.md
#
# Dependencies are NOT rebuilt: package.json keeps its remote_binary URL, so
# Decky downloads plugin-dependencies.tar.gz from the matching GitHub release.
# Python provider changes live in py_modules/providers and are included here.
#
# Usage:
#   ./build-plugin-zip.sh                 # pnpm build + zip
#   SKIP_BUILD=1 ./build-plugin-zip.sh    # reuse existing dist/
#   OUTPUT=/tmp/test.zip ./build-plugin-zip.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

PLUGIN_DIR_NAME="decky-translator-ru"
OUTPUT="${OUTPUT:-$ROOT/out/DeckyTranslatorRu.zip}"

for cmd in zip unzip; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "error: '$cmd' not found" >&2
        exit 1
    }
done

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
    command -v pnpm >/dev/null 2>&1 || {
        echo "error: pnpm not found (install it or pass SKIP_BUILD=1)" >&2
        exit 1
    }
    echo "==> pnpm build"
    pnpm build
fi

[[ -f dist/index.js ]] || {
    echo "error: dist/index.js not found; run pnpm build first" >&2
    exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

PLUGIN="$STAGE/$PLUGIN_DIR_NAME"
mkdir -p "$PLUGIN/py_modules"

cp -R dist "$PLUGIN/dist"
cp -R py_modules/providers "$PLUGIN/py_modules/providers"
cp main.py package.json plugin.json LICENSE README.md "$PLUGIN/"

find "$PLUGIN" -type d -name __pycache__ -exec rm -rf {} +
find "$PLUGIN" -name '*.pyc' -delete
find "$PLUGIN" -name '.DS_Store' -delete

mkdir -p "$(dirname "$OUTPUT")"
OUTPUT="$(cd "$(dirname "$OUTPUT")" && pwd)/$(basename "$OUTPUT")"
rm -f "$OUTPUT"
(cd "$STAGE" && zip -qr "$OUTPUT" "$PLUGIN_DIR_NAME")

echo
echo "Built: $OUTPUT"
ls -lh "$OUTPUT"
if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$OUTPUT"
else
    sha256sum "$OUTPUT"
fi
echo "Files: $(unzip -Z1 "$OUTPUT" | wc -l | tr -d ' ')"
