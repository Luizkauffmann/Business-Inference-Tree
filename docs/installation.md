# Installation

Tested on Dataiku DSS 15.1 with Python 3.11.

## 1. Code environment

Create a Python 3.11 code environment (e.g. `scr_binning`) with:

```
optbinning==0.20.1
scikit-learn==1.3.2
pandas>=2.0
numpy
flask
```

- `optbinning` is only needed by Strategy Tree Builder, for the optimal splits. Without it, the app falls back to a scikit-learn tree splitter and labels the split `tree`.
- `scikit-learn` is used for that fallback. The Visual ML import reads the tree structure through the Dataiku API and does not need it.

## 2. Project library

Copy the content of the three `src/` folders **flat** into the project library (`lib/python/`), keeping the two UI folders:

```
lib/python/
  strategy_engine.py   engine_examples.py   strategy_webapp.py   strategy_ui/{index.html, app.js, app.css}
  strategy_tree.py     tree_expr.py         tree_builder_webapp.py   tree_builder_ui/{index.html, app.js, app.css}
  binning_output.py
```

Both webapps follow a **thin-shell pattern**:
- The webapp itself holds a few lines of loader code.
- The real backend lives in the library: `register(app)` adds the Flask routes.
- The UI (`*_ui/`) is served by the `/ui` route and injected by the shell.

So a library change only needs a backend restart and a page refresh; the webapp definitions never change.

## 3. Managed folders

Create these managed folders in the project, on any connection. The names are fixed in the code:

| Folder | Used by | Content |
| --- | --- | --- |
| `decision_strategies` | both apps | `strategies/<NAME>.json` (Strategy Studio).<br>`trees/<NAME>.json` and `trees/history/<NAME>_v<n>.json` (Tree Builder).<br>`exports/` and `deployments/` |
| `binning_configs` | Tree Builder (optional) | Binning v2 configurations, offered as split definitions |

## 4. Webapps

Create two **standard webapps** (*Code webapp → Standard*), then fill the tabs from `webapp_shells/`:

| Tab | Strategy Studio | Strategy Tree Builder |
| --- | --- | --- |
| HTML | `strategy_studio/html.html` | `strategy_tree_builder/html.html` |
| CSS | `strategy_studio/css.css` | `strategy_tree_builder/css.css` |
| JS | `strategy_studio/js.js` | `strategy_tree_builder/js.js` |
| Python | `strategy_studio/backend.py` | `strategy_tree_builder/backend.py` |

Then, in the webapp **Settings**:
- **Backend**: enabled, Flask.
- **Code env**: the environment from step 1.
- **Container**: **None** (run locally). If the code environment is not built for containers, a containerised backend does not start.

Start the backend and open the webapp.

## 5. Data expected

- **Strategy Studio**: any dataset with a numeric **score** (higher = safer), ideally a 0/1 **target**. If the dataset was written by a scorecard webapp with the metadata keys `scorecard_cutoff`, `avg_margin_per_good` and `avg_loss_per_bad`, the economics and baseline cut-off are pre-filled.
- **Strategy Tree Builder**: any dataset with attributes and a 0/1 **target** (1 = event: default, repayment, fraud…), and ideally a score. Rows with an empty target are routed and scored but excluded from rates, IV and relevance.
- **Output datasets** (`<input>_strategy`, `<input>_tree`): written as managed datasets on the **input's connection**. An existing managed dataset is overwritten; the input never is.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Backend does not start: *code env not built for the selected runtime container* | Set container mode to None and restart |
| The page shows *Loading…* forever | Backend stopped: start it, then refresh |
| The first optimal split takes long | The first `import optbinning` takes up to a minute. The Tree Builder warms it up in the background when it starts and when data is loaded. |
| *Cannot read managed folder 'decision_strategies'* | Create the folder with exactly that name |
| Tree canvas empty | D3 v7 is loaded from cdnjs (fallback jsdelivr). Allow one of them in the browser / proxy. |
