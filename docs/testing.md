# Testing

The tests run outside Dataiku:
- a small **stub** of the `dataiku` package (`tests/stub/`) serves datasets, managed folders and a Visual ML tree from memory;
- **synthetic data** comes from `tests/make_demo_data.py`;
- the browser tests drive the real UI in headless Chrome with Playwright.

```bash
./tests/run_all.sh
```

The script:
1. copies `src/` flat into `build/lib` (the project-library layout);
2. generates `build/demo_applications.csv` (12,000 synthetic applications);
3. runs every suite.

It uses `uv` by default. To use another interpreter, set `PY="python"` with the packages installed: flask, playwright, pandas, numpy, scikit-learn 1.3.2, optbinning.

| Test | Checks |
| --- | --- |
| `engine_test.py` | Tree evaluation, metrics, guidance, optbinning splits.<br>SQL and Python exports agree with the engine on every row.<br>Studio conversion issues; relevance on 1,500 attributes (timing).<br>Visual ML import reproduces a scikit-learn tree's decisions on every row. |
| `pd_test.py` | Leaf event rate = node event rate at credibility 0; credibility shrinkage and intervals.<br>Freeze with holdout; scoring without a target; calibration.<br>SQL / Python exports reproduce `tree_proba_1` and `tree_prediction`; stale detection. |
| `wiki_examples.py` | The [Logic tab worked example](logic-and-formulas.md) validates, evaluates and exports identically to SQL and Python |
| `outputs_test.py` | Multiple outputs per leaf: default outputs, leaf values, decision from an output, segment-only leaves. App, SQL and Python agree on every column, with live and frozen leaf rates. |
| `studio_conv.py` | *Send to Studio* conversion gives identical decisions under Strategy Studio's semantics (missing branches, other categories) |
| `ui_test.py` (browser) | Load, relevance %, optimal split, debounced search, Binning v2 config, formulas, custom metrics, save, Send to Studio, write dataset, Visual ML import, 1,500-attribute virtualised list |
| `bins_test.py` (browser) | Bin table, multi-select merge (adjacent rule), split bin, edit boundary (valid range), missing routing, undo, full table, editing an existing split while keeping sub-trees, categorical merge / move, rule merge |
| `pd_ui_test.py` (browser) | KPI and status, leaf event-rate box and card metric, credibility 0, freeze with holdout, calibration, prediction columns in the written dataset, saved frozen rates, stale after editing a cut |
| `outputs_ui_test.py` (browser) | Default outputs on the root, inherited-outputs hint, decision-output picker, segment-only leaf, card chips, no-decision KPI, output columns in the written dataset, SQL export with outputs |
| `ss_shots.py` (browser) | Loads Strategy Studio, renders the tree, backtest and exports, and saves the screenshots used in the docs |

## Synthetic data

`make_demo_data.py` draws one latent risk factor that drives:
- bureau-style attributes (score, DTI, utilisation, inquiries, delinquencies, collections, credit age…) with some missing values;
- application attributes and a state effect;
- a policy score.

The 12-month outcome `bad_latent_synthetic` exists for every applicant. `bad_12m_flag_demo` keeps it only for approved applicants, reproducing the *booked loans only* situation of real credit data. No real data is used or shipped.
