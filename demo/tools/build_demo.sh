#!/usr/bin/env bash
# Copies the webapp sources into docs/demo/app, the folder served by GitHub Pages.
# The demo runs these exact files in the browser (Pyodide); run this after changing src/.
set -euo pipefail
cd "$(dirname "$0")/../.."
OUT=docs/demo/app
rm -rf "$OUT" && mkdir -p "$OUT"
cp -R src/strategy_tree_builder/. src/strategy_studio/. src/common/. "$OUT"/
find "$OUT" -name "__pycache__" -prune -exec rm -rf {} +
echo "demo app refreshed in $OUT:"; ls "$OUT"
