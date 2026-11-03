# Architecture and API

## Thin-shell webapps

```
Dataiku standard webapp
 ├─ JS tab      fetch('/ui') → inject CSS + HTML, run the app JS
 └─ Python tab  from <module> import register; register(app)     (Flask)
                    │
project library     ▼
 ├─ strategy_webapp.py      routes ──► strategy_engine.py (+ engine_examples.py)
 ├─ tree_builder_webapp.py  routes ──► strategy_tree.py ──► tree_expr.py
 └─ binning_output.py       writes output datasets on the input's connection
```

- **Pure engines.** `strategy_engine.py` and `strategy_tree.py` are pure pandas / numpy, with no Dataiku import. They are unit-testable and reusable from recipes, scenarios or agent tools.
- **Webapp modules** add the Dataiku I/O: datasets, managed folders, the Visual ML API and the current user.
- **State.** Each backend keeps **one loaded population in memory**, shared by every user of that webapp backend. Loading a dataset replaces it. Duplicate the webapp for teams that need separate sessions.

## Modules

| Module | Responsibility |
| --- | --- |
| `strategy_engine.py` | Binary strategy model, validation, evaluation, backtest, champion / challenger, templates, exporters (InterConnect, PowerCurve, FICO mock, generic, SQL, Python) and importers |
| `engine_examples.py` | Example exports for Oscilar, Pega, Provenir, Taktile and DMN 1.3 (rule flattening + per-engine expression dialects) |
| `strategy_webapp.py` | Strategy Studio routes |
| `strategy_tree.py` | N-ary tree model, routing, evaluation, node metrics, `Prebin` relevance, optbinning suggestions, bin table, Binning v2 config splits, Visual ML import, event-rate prediction / calibration, exporters, Studio conversion |
| `tree_expr.py` | Safe formula language: parse, vectorised evaluation, translation to SQL and Python |
| `tree_builder_webapp.py` | Strategy Tree Builder routes |
| `binning_output.py` | Creates or overwrites a managed output dataset on the input's connection (falls back to `filesystem_managed`) |

## Strategy Studio routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/ui` | GET | HTML, CSS and JS of the app |
| `/api/datasets` | GET | Project datasets |
| `/api/dataset_info?dataset=` | GET | Columns + scorecard metadata (cut-off, margin, loss) |
| `/api/load` | POST | Load a population: `dataset`, `score_col`, `target` |
| `/api/template` | POST | Starter trees (`cutoff_only`, `delinquency_override`) |
| `/api/evaluate` | POST | Validate + backtest a tree (KPIs, per-node stats, baseline) |
| `/api/export` | POST | Export in a `format` (see [Exports](exports.md)) |
| `/api/import` | POST | Tree from a generic / InterConnect / PowerCurve JSON |
| `/api/strategy/list`, `/api/strategy/save`, `/api/strategy/load` | GET / POST | Saved strategies in `decision_strategies/strategies/` |
| `/api/compare` | POST | Champion vs challenger with the swap set |
| `/api/save_export` | POST | Save an export to `decision_strategies/exports/` |
| `/api/deploy`, `/api/deployments` | POST / GET | Simulated InterConnect deployment and its receipts |
| `/api/write_dataset` | POST | Write `<input>_strategy` |

## Strategy Tree Builder routes

| Route | Method | Purpose |
| --- | --- | --- |
| `/ui` | GET | HTML, CSS and JS of the app |
| `/api/datasets`, `/api/columns` | GET | Datasets, columns |
| `/api/load` | POST | Load a population, pre-bin attributes, starter tree |
| `/api/evaluate` | POST | Validate + evaluate: node metrics, summary KPIs, event-rate report (status, calibration, leaf rates) |
| `/api/relevance` | POST | All attributes ranked for one node |
| `/api/suggest` | POST | optbinning split of one variable in one node + bin table |
| `/api/preview` | POST | Bin table / branch stats of any split definition |
| `/api/guidance` | POST | Guided next splits |
| `/api/column_values` | GET | Top values of a column |
| `/api/binning_configs`, `/api/split_from_config` | GET / POST | Binning v2 configurations as splits |
| `/api/vml_models`, `/api/vml_import` | GET / POST | Visual ML decision-tree import |
| `/api/freeze_pd` | POST | Leaf event rates to store in the tree |
| `/api/trees`, `/api/tree`, `/api/save` | GET / POST | Saved trees in `decision_strategies/trees/` (versioned) |
| `/api/export` | POST | JSON / SQL / Python / Studio |
| `/api/send_to_studio` | POST | Convert and write to `decision_strategies/strategies/` (asks for confirmation when simplified) |
| `/api/write_dataset` | POST | Write `<input>_tree` |

## Data contracts

### Strategy Studio tree (binary)

```json
{"id": "n1", "type": "split", "name": "Score above cut-off", "variable": "score",
 "op": ">=", "value": 640, "value2": null, "missing": "no",
 "yes": {"id": "n2", "type": "leaf", "name": "Approve",
         "score_adjust": {"type": "pct", "value": -10},
         "decision": {"mode": "cutoff", "approve_at": 640, "refer_at": 620},
         "reason_codes": ["DELINQ_24M"], "pricing_tier": "C", "limit_multiplier": 0.8},
 "no":  {"id": "n3", "type": "leaf", "name": "Decline", "decision": {"mode": "decline"}, "...": "..."}}
```

- **Operators**: `< <= > >= == != in not_in between is_missing`.
- **Score adjustment `type`**: `none | pct | points | set`.
- **Decision `mode`**: `cutoff | approve | refer | decline`.

Saved document (`strategies/<NAME>.json`): `{meta: {name, strategy_id, status, version, description}, tree, settings: {margin, loss, baseline_cutoff}, saved_at, saved_by, history: [...]}`.

### Strategy Tree Builder tree (n-ary)

```json
{"id": "n1", "name": "All applications",
 "calcs": [{"name": "adj_score", "expr": "policy_score_existing - 15 * inquiries_6m_count"}],
 "split": {"kind": "numeric", "variable": "vantage_score", "cuts": [643.5, 686.5, 792.5],
           "missing_branch": 0, "source": "optbinning"},
 "children": [
   {"id": "n2", "name": "vantage_score < 643.5", "calcs": [], "split": null, "children": [],
    "leaf": {"mode": "cutoff", "score_expr": "adj_score", "approve_at": 760, "refer_at": 740,
             "reason_codes": ["LOW_SCORE"], "pricing_tier": "C", "limit_expr": "0.8",
             "outputs": [{"name": "apr", "expr": "0.199 if vantage_score < 640 else 0.149"}]}}]}
```

**Split kinds**

| `kind` | Fields | Branches |
| --- | --- | --- |
| `numeric` | `cuts` | k + 1: `x < c1`, `c1 ≤ x < c2`, …, `x ≥ ck` |
| `categorical` | `groups`, `other_branch` or `other_separate` | One per group (+ *Other*) |
| `rule` | `rules` | One per rule, first true wins, + *Otherwise* |

- **Missing values**: `missing_branch` (index) or `missing_separate: true`, which adds a last *Missing* branch.
- **`source`**: `manual | optbinning | tree | binning_config:<name> | visual_ml`.

**Tree root options**
- `pd_settings`: `{credibility, threshold, holdout_pct, seed}`.
- `pd_model` (frozen leaf rates): `{status, frozen_at, frozen_by, dataset, target, n_known, base_rate, credibility, threshold, holdout_pct, seed, signature, leaves: {leaf_id: {n_known, bads, raw, pd, lower, upper}}}`.

Saved document (`trees/<NAME>.json`): `{type: "dataiku_strategy_tree_v2", meta, tree, display: {metrics, custom_metrics}, settings: {margin, loss}, saved_at, saved_by}`.

### Output columns

| Dataset | Columns added to the input |
| --- | --- |
| `<input>_strategy` (Studio) | `strategy_leaf`, `strategy_path`, `adjusted_score`, `strategy_decision`, `reason_codes`, `pricing_tier`, `limit_multiplier` |
| `<input>_tree` (Tree Builder) | `tree_leaf`, `tree_path`, `tree_score`, `tree_decision`, `reason_codes`, `pricing_tier`, `limit_multiplier`, `out_<name>`, `calc_<name>`, `tree_proba_1`, `tree_proba_0`, `tree_prediction`, `tree_event_rate`, `tree_event_rate_raw`, `tree_event_rate_lower`, `tree_event_rate_upper`, `tree_event_n_known`, `tree_event_rate_source` |

## Relevance (`Prebin`)

1. **Code once.** Each candidate attribute is coded into ≤ 30 classes (`uint8`):
   - numeric: up to 20 quantile bins, or one class per value when there are ≤ 20 distinct values;
   - categorical: the 29 most frequent categories plus *other* (`OTHER_CODE = 30`);
   - missing: `MISS_CODE = 31`.
2. **Rank per node.** A node's relevance is computed from chunked bincounts of codes × target on the node's rows with a known outcome.
   - **IV** uses 0.5 smoothing.
   - **Information gain** = (H(node) − Σ weighted H(class)) / H(node).
   - **Relevance %** = gain / best gain.
3. **Flags.** Attributes are flagged as:
   - *outcome window*: missing exactly where the target is missing;
   - *ID-like*, *constant*, *high missing* (> 50 %), *date*;
   - *decision output*: e.g. the existing decision or a surrogate model's prediction;
   - *suspicious*: IV ≥ 2.

   Flagged attributes are hidden by default (*Hide flagged*). Outcome-window, ID-like and constant attributes are also left out of the guided next splits.
