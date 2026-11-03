#!/usr/bin/env bash
# Runs every test against a flat copy of src/ (the layout of a Dataiku project library).
# Needs Python 3.11, Google Chrome (browser tests) and either `uv` or the packages below.
set -euo pipefail
cd "$(dirname "$0")/.."
rm -rf build && mkdir -p build/lib build/out
cp -R src/strategy_studio/. src/strategy_tree_builder/. src/common/. build/lib/
PY=${PY:-"uv run -q --no-project --with flask --with playwright --with pandas --with numpy --with scikit-learn==1.3.2 --with optbinning python"}
DATA=build/demo_applications.csv
$PY tests/make_demo_data.py "$DATA"
echo "== engine";        $PY tests/engine_test.py build/lib "$DATA"
echo "== event rate";    $PY tests/pd_test.py build/lib "$DATA"
echo "== logic example"; $PY tests/wiki_examples.py build/lib "$DATA"
echo "== outputs";       $PY tests/outputs_test.py build/lib "$DATA"
echo "== studio conv";   $PY tests/studio_conv.py build/lib "$DATA"
echo "== UI";            $PY tests/ui_test.py build/lib tests/stub build/out "$DATA"
echo "== bin editor";    $PY tests/bins_test.py build/lib tests/stub build/out "$DATA"
echo "== event-rate UI"; $PY tests/pd_ui_test.py build/lib tests/stub build/out "$DATA"
echo "== outputs UI";    $PY tests/outputs_ui_test.py build/lib tests/stub build/out "$DATA"
echo "== DP binning";      $PY tests/dp_test.py build/lib "$DATA"
echo "== browser demo";   demo/tools/build_demo.sh > /dev/null && $PY tests/demo_browser_test.py build/out
echo "== Strategy Studio screenshots"; $PY tests/ss_shots.py build/lib tests/stub build/out "$DATA"
