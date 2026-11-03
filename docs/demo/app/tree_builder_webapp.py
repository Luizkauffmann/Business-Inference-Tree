"""Flask routes for the Strategy Tree Builder webapp (Strategy Studio + optimal binning).

The webapp's Python tab only needs:

    from tree_builder_webapp import register
    register(app)

Tree logic lives in ``strategy_tree`` (n-ary splits, node calculations, metrics, relevance,
optbinning suggestions, Visual ML import, exporters) and formulas in ``tree_expr``.
Trees are saved to the ``decision_strategies`` folder under /trees/; "Send to Strategy
Studio" writes a converted binary tree under /strategies/ (Strategy Studio's format).
"""

import json
import math
import os
import threading
import time
import traceback

import dataiku
import numpy as np
import pandas as pd
from flask import jsonify, request

import strategy_tree as st
import tree_expr as tx

STRATEGY_FOLDER = "decision_strategies"
BINNING_FOLDER = "binning_configs"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tree_builder_ui")
PREBIN_MAX_ROWS = 200000
# decision outputs / identifiers: still listed, flagged so they are hidden from guidance by default
DECISION_OUTPUTS = {"existing_decision", "prediction", "proba_Approve", "proba_Decline", "primary_reason_codes",
                    "existing_pricing_tier", "offered_apr_pct", "assigned_credit_limit_or_loan_amount",
                    "manual_review_flag", "strategy_name", "interconnect_request_id", "loan_id"}
HIDE_IN_GUIDANCE = {"outcome_window", "id_like", "constant", "decision_output", "date", "target"}

_lock = threading.RLock()
_S = {}


def clean(o):
    if isinstance(o, dict):
        return {str(k): clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, (np.floating, float)):
        f = float(o)
        return f if math.isfinite(f) else None
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.ndarray):
        return clean(o.tolist())
    return o


def ok(**payload):
    return jsonify(clean(payload))


def fail(e, code=500):
    if code == 500:
        traceback.print_exc()
    return jsonify(error=str(e)), code


def need():
    if _S.get("df") is None:
        raise ValueError("Load a population first.")
    return _S


def _browser_user():
    try:
        return dataiku.api_client().get_auth_info_from_browser_headers(dict(request.headers)).get("authIdentifier", "")
    except Exception:
        return ""


def _warm_optbinning():
    try:
        import optbinning  # noqa: F401  (first import takes several seconds: do it before the first click)
        _S["optbinning"] = True
    except Exception:
        _S["optbinning"] = False


def _is_date(col, name):
    if pd.api.types.is_datetime64_any_dtype(col):
        return True
    if any(k in name.lower() for k in ("date", "month", "_dt", "timestamp")) and not pd.api.types.is_numeric_dtype(col):
        return True
    return False


def _evaluate(tree, display=None):
    s = need()
    c = s["cfg"]
    res, masks, env = st.evaluate_full(s["df"], tree, c.get("score_col"), s["y"], c["dataset"])
    metrics = st.node_metrics(s["df"], tree, res, masks, env, c.get("target"), c.get("margin"), c.get("loss"),
                              (display or {}).get("custom_metrics"))
    res, info = st.predict(res, tree, masks, s["y"], c["dataset"])
    if "tree_proba_1" in res:
        p1 = res["tree_proba_1"].values.astype(float)
        for nid, m in masks.items():
            v = p1[m]
            v = v[~np.isnan(v)]
            if nid in metrics:
                metrics[nid]["pd"] = float(v.mean()) if len(v) else None
    return res, masks, env, metrics, info


def _pd_report(tree, res, info):
    """Event-rate prediction status + calibration, on the holdout rows when there is one (else all rows, flagged in-sample)."""
    s = need()
    stg = st.pd_settings(tree)
    pm = tree.get("pd_model") or {}
    if info.get("state") == "frozen":
        same = pm.get("dataset") == s["cfg"]["dataset"]
        hp, seed = (pm.get("holdout_pct") or 0), pm.get("seed", 42)
    else:
        same, hp, seed = True, stg["holdout_pct"], stg["seed"]
    rows, scope = None, "all rows (out-of-sample: different dataset)" if not same else "all rows (in-sample)"
    if same and float(hp or 0) > 0:
        rows, scope = st.holdout_mask(len(s["df"]), hp, seed), f"{hp}% holdout (not used for the event rates)"
    cal = st.calibration(res, s["y"], tree, rows)
    return dict(state=info.get("state"), source=info.get("source"), threshold=info.get("threshold"), settings=stg,
                frozen={k: pm.get(k) for k in ("dataset", "target", "frozen_at", "frozen_by", "n_known", "base_rate",
                                                "credibility", "threshold", "holdout_pct")} if pm else None,
                calibration=cal, calibration_scope=scope, leaves=info.get("leaves"))


def _node_mask(tree, node_id):
    res, masks, env = st.evaluate(need()["df"], tree, need()["cfg"].get("score_col"))
    if node_id not in masks:
        raise ValueError(f"Node {node_id} not found.")
    return masks[node_id], env, masks


def _calc_series(tree, env):
    return {k: v for k, v in env.calcs.items()}


def _folder(name):
    return dataiku.Folder(name)


def _read_json(folder, path):
    with folder.get_download_stream(path) as f:
        return json.loads(f.read().decode("utf-8"))


def _write_json(folder, path, doc):
    with folder.get_writer(path) as w:
        w.write(json.dumps(clean(doc), indent=2).encode("utf-8"))


def register(app):
    threading.Thread(target=_warm_optbinning, daemon=True).start()

    @app.route("/ui")
    def ui():
        out = {}
        for key, name in (("html", "index.html"), ("css", "app.css"), ("js", "app.js")):
            with open(os.path.join(UI_DIR, name), encoding="utf-8") as f:
                out[key] = f.read()
        return jsonify(out)

    @app.route("/api/datasets")
    def api_datasets():
        try:
            return ok(datasets=sorted(d["name"] for d in dataiku.api_client().get_default_project().list_datasets()))
        except Exception as e:
            return fail(e)

    @app.route("/api/columns")
    def api_columns():
        try:
            return ok(columns=[c["name"] for c in dataiku.Dataset(request.args["dataset"]).read_schema()])
        except Exception as e:
            return fail(e)

    @app.route("/api/load", methods=["POST"])
    def api_load():
        b = request.get_json() or {}
        try:
            with _lock:
                sample_n = b.get("sample_rows")
                sample_n = int(float(sample_n)) if sample_n not in (None, "", 0, "0") else None
                ds_ = dataiku.Dataset(b["dataset"])
                # blank = every row in memory (default); a number = random sample for interactive design
                df = ds_.get_dataframe(sampling="random", limit=sample_n) if sample_n else ds_.get_dataframe()
                df = df.reset_index(drop=True)
                target, score = b.get("target"), b.get("score_col")
                for k, v in (("target", target), ("score column", score)):
                    if v and v not in df.columns:
                        raise ValueError(f"The {k} '{v}' is not in the dataset.")
                cfg = dict(dataset=b["dataset"], target=target, score_col=score, sample_rows=sample_n,
                           margin=float(b["margin"]) if b.get("margin") not in (None, "") else None,
                           loss=float(b["loss"]) if b.get("loss") not in (None, "") else None)
                y = st.target_array(df, target) if target else None
                excluded = {target} | {c for c in df.columns if c.startswith("bad_") and c != target and c.endswith(("_synthetic", "_demo", "_flag"))}
                cands = [c for c in df.columns if c not in excluded]
                rows = np.arange(len(df))
                if len(df) > PREBIN_MAX_ROWS:
                    rows = np.sort(np.random.RandomState(7).choice(len(df), PREBIN_MAX_ROWS, replace=False))
                pb = st.Prebin(df.iloc[rows], cands, target=target)
                for c in cands:
                    if c in DECISION_OUTPUTS:
                        pb.flags[c].append("decision_output")
                    if _is_date(df[c], c):
                        pb.flags[c].append("date")
                for c in excluded & set(df.columns):
                    pass
                _S.clear()
                _S.update(df=df, y=y, cfg=cfg, prebin=pb, prebin_rows=rows, optbinning=_S.get("optbinning"))
                threading.Thread(target=_warm_optbinning, daemon=True).start()
                prof = []
                for c in df.columns:
                    col = df[c]
                    numeric = pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col)
                    prof.append(dict(name=c, kind="numeric" if numeric else "categorical",
                                     missing=round(float(col.isna().mean()) * 100, 1),
                                     flags=pb.flags.get(c, ["target"] if c in excluded else [])))
                cutoff = None
                if score:
                    sc = pd.to_numeric(df[score], errors="coerce")
                    cutoff = float(sc.quantile(0.3)) if sc.notna().any() else None
                return ok(n=len(df), n_known=int((~np.isnan(y)).sum()) if y is not None else None,
                          bad_rate=float(np.nanmean(y)) if y is not None else None, columns=prof,
                          starter=st.starter_tree(score, cutoff) if score else st.new_leaf("n1", "All applications", mode="approve"),
                          prebin_rows=len(rows), cfg=cfg, functions=tx.FUNCTIONS, metric_labels=st.METRIC_LABELS,
                          default_metrics=st.DEFAULT_METRICS)
        except Exception as e:
            return fail(e)

    @app.route("/api/evaluate", methods=["POST"])
    def api_evaluate():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                issues = st.validate(b["tree"], s["df"].columns, s["cfg"].get("score_col"))
                if any(i["level"] == "error" for i in issues):
                    return ok(issues=issues, metrics=None, summary=None)
                res, masks, env, metrics, info = _evaluate(b["tree"], b.get("display"))
                return ok(issues=issues, metrics=metrics,
                          summary=st.summary(res, s["df"], s["cfg"].get("target"), s["cfg"].get("margin"), s["cfg"].get("loss")),
                          pd=_pd_report(b["tree"], res, info))
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    @app.route("/api/relevance", methods=["POST"])
    def api_relevance():
        """All attributes ranked for one node (relevance %, IV, information gain, flags)."""
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                if s["y"] is None:
                    return fail("Choose a target (1 = event) to compute relevance scores.", 400)
                mask, env, _ = _node_mask(b["tree"], b["node_id"])
                rows = s["prebin_rows"]
                extra = {k: v.iloc[rows] for k, v in env.calcs.items()} if env.calcs else None
                if extra:
                    extra = {k: v.reset_index(drop=True) for k, v in extra.items()}
                rel = s["prebin"].relevance(mask[rows], s["y"][rows], extra=extra)
                kn = mask & ~np.isnan(s["y"])
                return ok(node_id=b["node_id"], attributes=rel, n=int(mask.sum()), n_known=int(kn.sum()),
                          bad_rate=float(np.nanmean(s["y"][kn])) if kn.any() else None,
                          sampled=len(rows) < len(s["df"]))
        except Exception as e:
            return fail(e)

    @app.route("/api/suggest", methods=["POST"])
    def api_suggest():
        """Optimal-binning split of one variable inside one node, with its branch preview."""
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                mask, env, _ = _node_mask(b["tree"], b["node_id"])
                v = b["variable"]
                if v not in env:
                    raise ValueError(f"Unknown variable '{v}'.")
                x = env[v][mask]
                x.name = v
                split = st.suggest_split(x, s["y"][mask], kind=b.get("kind"), max_bins=int(b.get("max_bins", 4)),
                                         min_share=float(b.get("min_share", 0.05)), monotonic=b.get("monotonic", "auto"),
                                         missing=b.get("missing", "auto"))
                prev = st.branch_stats(split, env, s["df"].index, mask, s["y"])
                return ok(split=split, preview=prev, labels=st.branch_labels(split))
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    @app.route("/api/preview", methods=["POST"])
    def api_preview():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                mask, env, _ = _node_mask(b["tree"], b["node_id"])
                split = b["split"]
                probe = {"id": "_p", "split": split, "children": [st.new_leaf(f"_c{i}") for i in range(st.n_branches(split))]}
                known = set(s["df"].columns) | set(env.calcs)
                issues = [i for i in st.validate(probe, known, "x") if i["node_id"] == "_p"]
                if issues:
                    return fail("; ".join(i["message"] for i in issues), 400)
                return ok(preview=st.branch_stats(split, env, s["df"].index, mask, s["y"]), labels=st.branch_labels(split))
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    @app.route("/api/guidance", methods=["POST"])
    def api_guidance():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                if s["y"] is None:
                    return ok(steps=[])
                res, masks, env = st.evaluate(s["df"], b["tree"], s["cfg"].get("score_col"))
                rows = s["prebin_rows"]
                pb = s["prebin"]
                steps = []
                for lf in st.leaves(b["tree"]):
                    m = masks[lf["id"]][rows]
                    kn = m & ~np.isnan(s["y"][rows])
                    if kn.sum() < int(b.get("min_known", 100)):
                        continue
                    rel = [r for r in pb.relevance(m, s["y"][rows]) if not set(r["flags"]) & HIDE_IN_GUIDANCE]
                    if rel:
                        steps.append(dict(node_id=lf["id"], name=lf.get("name") or lf["id"], n_known=int(kn.sum()),
                                          bad_rate=float(np.nanmean(s["y"][rows][kn])), variable=rel[0]["variable"],
                                          iv=rel[0]["iv"], gain=rel[0]["gain"], value=float(kn.sum() * rel[0]["gain"]),
                                          runners_up=[r["variable"] for r in rel[1:4]]))
                steps.sort(key=lambda r: -r["value"])
                return ok(steps=steps[:5])
        except Exception as e:
            return fail(e)

    @app.route("/api/column_values")
    def api_column_values():
        try:
            s = need()
            col = s["df"][request.args["column"]]
            if pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col):
                q = col.quantile([0, .1, .25, .5, .75, .9, 1]).tolist()
                return ok(kind="numeric", quantiles=q, missing=float(col.isna().mean()))
            vc = col.astype(str).where(col.notna(), None).value_counts().head(200)
            return ok(kind="categorical", values=[dict(value=k, n=int(v)) for k, v in vc.items()],
                      missing=float(col.isna().mean()))
        except Exception as e:
            return fail(e)

    # ── Binning v2 saved configurations as split definitions ──

    @app.route("/api/binning_configs")
    def api_binning_configs():
        try:
            folder = _folder(BINNING_FOLDER)
            out = []
            for p in folder.list_paths_in_partition():
                if not p.endswith(".json"):
                    continue
                try:
                    cfg = _read_json(folder, p)
                except Exception:
                    continue
                if cfg.get("type") != "scorecard_binning_config":
                    continue
                out.append(dict(path=p, name=cfg.get("name") or p, target=cfg.get("target"), saved_at=cfg.get("saved_at"),
                                variables=sorted((cfg.get("variables") or {}).keys())))
            return ok(configs=sorted(out, key=lambda c: c.get("saved_at") or "", reverse=True))
        except Exception as e:
            return fail(e)

    @app.route("/api/split_from_config", methods=["POST"])
    def api_split_from_config():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                cfg = _read_json(_folder(BINNING_FOLDER), b["path"])
                v = b["variable"]
                vc = (cfg.get("variables") or {}).get(v)
                if not vc:
                    raise ValueError(f"'{v}' is not in that binning configuration.")
                mask, env, _ = _node_mask(b["tree"], b["node_id"])
                x = env[v][mask]
                x.name = v
                split = st.split_from_binning_config(vc, v, x, s["y"][mask] if s["y"] is not None else None, cfg.get("name", ""))
                return ok(split=split, preview=st.branch_stats(split, env, s["df"].index, mask, s["y"]),
                          labels=st.branch_labels(split))
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    # ── Visual ML Lab decision trees ──

    @app.route("/api/vml_models")
    def api_vml_models():
        try:
            p = dataiku.api_client().get_default_project()
            out = []
            for a in p.list_analyses():
                aid = a.get("analysisId")
                try:
                    an = p.get_analysis(aid)
                    tasks = an.list_ml_tasks()
                    tasks = tasks.get("mlTasks", []) if isinstance(tasks, dict) else tasks
                except Exception:
                    continue
                for t in tasks:
                    tid = t.get("mlTaskId")
                    try:
                        mt = an.get_ml_task(tid)
                        for mid in mt.get_trained_models_ids():
                            sn = mt.get_trained_model_snippet(mid)
                            if sn.get("algorithm") in ("DECISION_TREE_CLASSIFICATION", "RANDOM_FOREST_CLASSIFICATION"):
                                out.append(dict(kind="lab", analysis_id=aid, mltask_id=tid, model_id=mid,
                                                algorithm=sn.get("algorithm"),
                                                label=f"{a.get('analysisName', aid)} · {(sn.get('userMeta') or {}).get('name', mid)}",
                                                auc=sn.get("auc"), dataset=a.get("inputDatasetSmartName")))
                    except Exception:
                        continue
            for sm in p.list_saved_models():
                try:
                    smo = p.get_saved_model(sm["id"])
                    for v in smo.list_versions():
                        algo = (v.get("snippet") or {}).get("algorithm")
                        if algo in ("DECISION_TREE_CLASSIFICATION", "RANDOM_FOREST_CLASSIFICATION"):
                            out.append(dict(kind="saved", saved_model_id=sm["id"], version_id=v["id"], algorithm=algo,
                                            label=f"Saved model {sm.get('name', sm['id'])} · {v['id']}{' (active)' if v.get('active') else ''}"))
                except Exception:
                    continue
            return ok(models=out)
        except Exception as e:
            return fail(e)

    @app.route("/api/vml_import", methods=["POST"])
    def api_vml_import():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                p = dataiku.api_client().get_default_project()
                if b.get("kind") == "saved":
                    details = p.get_saved_model(b["saved_model_id"]).get_version_details(b["version_id"])
                    label = f"saved model {b['saved_model_id']} v{b['version_id']}"
                else:
                    details = p.get_analysis(b["analysis_id"]).get_ml_task(b["mltask_id"]).get_trained_model_details(b["model_id"])
                    label = b["model_id"]
                raw = details.get_trees().get_raw()
                if not raw.get("classes"):
                    try:
                        rm = details.get_preprocessing_settings().get("target_remapping") or []
                        raw["classes"] = [str(r["sourceValue"]) for r in sorted(rm, key=lambda r: r["mappedValue"])]
                    except Exception:
                        pass
                tree, report = st.from_dss_trees(raw, s["df"], approve_class=b.get("approve_class") or None,
                                                 threshold=float(b.get("threshold", 0.5)),
                                                 tree_index=int(b.get("tree_index", 0)), model_label=label)
                if s["cfg"].get("score_col"):
                    report["note"] = ("Leaves decide from the model probability; switch a leaf to 'cut-off' to use "
                                      f"{s['cfg']['score_col']} instead.")
                return ok(tree=tree, report=report)
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    # ── save / load / export ──

    @app.route("/api/trees")
    def api_trees():
        try:
            folder = _folder(STRATEGY_FOLDER)
            out = []
            for p in folder.list_paths_in_partition():
                if p.startswith("/trees/") and p.endswith(".json") and "/history/" not in p:
                    try:
                        d = _read_json(folder, p)
                        m = d.get("meta") or {}
                        out.append(dict(path=p, name=m.get("name"), version=m.get("version"), status=m.get("status"),
                                        saved_at=d.get("saved_at"), saved_by=d.get("saved_by"), dataset=m.get("dataset")))
                    except Exception:
                        continue
            return ok(trees=sorted(out, key=lambda r: r.get("saved_at") or "", reverse=True))
        except Exception as e:
            return fail(e)

    @app.route("/api/tree")
    def api_tree():
        try:
            return ok(**_read_json(_folder(STRATEGY_FOLDER), request.args["path"]))
        except Exception as e:
            return fail(e)

    @app.route("/api/freeze_pd", methods=["POST"])
    def api_freeze_pd():
        """Leaf event rates from the loaded population, to store in the tree (tree["pd_model"])."""
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                tree = b["tree"]
                if b.get("settings"):
                    tree = dict(tree, pd_settings=b["settings"])
                _, masks, _ = st.evaluate(s["df"], tree, s["cfg"].get("score_col"))
                pm = st.freeze_pd(tree, masks, s["y"], s["cfg"]["dataset"], s["cfg"].get("target") or "", _browser_user())
                return ok(pd_model=pm)
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    def _sql_dialect_of(name, table):
        """'ansi' | 'bigquery' | 'mysql' | None (not supported) from the dataset's connection type."""
        try:
            t = (dataiku.api_client().get_default_project().get_dataset(name).get_settings().get_raw().get("type") or "").lower()
        except Exception:
            t = ""
        if "bigquery" in t:
            return "bigquery"
        if t in ("mysql", "mariadb"):
            return "mysql"
        if t in ("sqlserver", "synapse", "azuresql", "fabricwarehouse") or table.startswith("["):
            return None
        if table.startswith("`"):
            return "bigquery"
        return "ansi"

    @app.route("/api/backtest_db", methods=["POST"])
    def api_backtest_db():
        """Runs the tree's SQL export inside the database on the full table (only aggregates come back)
        and compares the KPIs with the in-memory population (full or sampled)."""
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                c, tree = s["cfg"], b["tree"]
                ds = dataiku.Dataset(c["dataset"])
                try:
                    loc = ds.get_location_info() or {}
                except Exception:
                    loc = {}
                info = loc.get("info") or {}
                table = info.get("quotedResolvedTableName")
                if loc.get("locationInfoType") != "SQL" or not table:
                    return fail(f"In-database backtest needs a dataset stored in a SQL database (Snowflake, PostgreSQL…); "
                                f"'{c['dataset']}' is not. Use the in-memory KPIs instead.", 400)
                dialect = _sql_dialect_of(c["dataset"], table)
                if dialect is None:
                    return fail("In-database backtest does not support SQL Server / Synapse yet; "
                                "use the in-memory KPIs or a Snowflake / BigQuery / PostgreSQL copy.", 400)
                errors = [i for i in st.validate(tree, s["df"].columns, c.get("score_col")) if i["level"] == "error"]
                if errors:
                    return fail("Fix the tree first: " + errors[0]["message"], 400)
                res, masks, _ = st.evaluate_full(s["df"], tree, c.get("score_col"), s["y"], c["dataset"])
                rates, src = None, None
                if st.uses_leaf_rates(tree) and st.pd_state(tree) != "frozen":
                    rates, _, src, _ = st.rate_table(tree, masks, s["y"], c["dataset"])
                res, _ = st.predict(res, tree, masks, s["y"], c["dataset"])
                sql = st.backtest_sql(tree, dict(name=(b.get("meta") or {}).get("name"), score_col=c.get("score_col")),
                                      table, c.get("target"), rates, src, dialect=dialect,
                                      numeric_columns=[k for k in s["df"].columns if pd.api.types.is_numeric_dtype(s["df"][k])
                                                       and not pd.api.types.is_bool_dtype(s["df"][k])])
                t0 = time.time()
                db = dataiku.SQLExecutor2(dataset=ds).query_to_df(sql)
                secs = time.time() - t0
                mem = st.backtest_groups(res, s["df"], c.get("target"))
                return ok(db=st.backtest_summary(db, c.get("margin"), c.get("loss")),
                          mem=st.backtest_summary(mem, c.get("margin"), c.get("loss")),
                          leaves=st.backtest_leaves(mem, db), sql=sql, seconds=secs, table=table,
                          sample_rows=c.get("sample_rows"), rows_in_memory=len(s["df"]),
                          rates_note=src, dialect=dialect)
        except (st.TreeError, tx.ExprError) as e:
            return fail(e, 400)
        except Exception as e:
            return fail(e)

    @app.route("/api/save", methods=["POST"])
    def api_save():
        b = request.get_json() or {}
        try:
            s = need()
            name = st.safe_name((b.get("meta") or {}).get("name") or "STRATEGY_TREE")
            folder = _folder(STRATEGY_FOLDER)
            path = f"/trees/{name}.json"
            version = 1
            try:
                version = int((_read_json(folder, path).get("meta") or {}).get("version") or 0) + 1
            except Exception:
                pass
            meta = dict(b.get("meta") or {}, name=name, version=version, dataset=s["cfg"]["dataset"],
                        score_col=s["cfg"].get("score_col"), target=s["cfg"].get("target"))
            doc = dict(type="dataiku_strategy_tree_v2", meta=meta, tree=b["tree"], display=b.get("display") or {},
                       settings=dict(margin=s["cfg"].get("margin"), loss=s["cfg"].get("loss")),
                       saved_at=st.now(), saved_by=_browser_user())
            _write_json(folder, path, doc)
            _write_json(folder, f"/trees/history/{name}_v{version}.json", doc)
            return ok(path=path, version=version, name=name)
        except Exception as e:
            return fail(e)

    @app.route("/api/export", methods=["POST"])
    def api_export():
        b = request.get_json() or {}
        try:
            s = need()
            meta = dict(b.get("meta") or {}, score_col=s["cfg"].get("score_col"), dataset=s["cfg"]["dataset"])
            fmt = b.get("format", "json")
            rates, src = None, None
            if fmt in ("sql", "python") and st.uses_leaf_rates(b["tree"]) and st.pd_state(b["tree"]) != "frozen":
                _, masks, _ = st.evaluate(s["df"], b["tree"], s["cfg"].get("score_col"))
                rates, _, src, _ = st.rate_table(b["tree"], masks, s["y"], s["cfg"]["dataset"])
            if fmt == "sql":
                body, ext = st.to_sql(b["tree"], meta, leaf_rates=rates, rates_source=src), "sql"
            elif fmt == "python":
                body, ext = st.to_python(b["tree"], meta, leaf_rates=rates, rates_source=src), "py"
            elif fmt == "studio":
                tree, issues = st.to_studio(b["tree"], meta)
                body, ext = json.dumps(dict(meta=meta, tree=tree, conversion_issues=issues), indent=2), "json"
            else:
                body, ext = json.dumps(clean(st.to_generic(b["tree"], meta)), indent=2), "json"
            return ok(body=body, ext=ext)
        except Exception as e:
            return fail(e)

    @app.route("/api/send_to_studio", methods=["POST"])
    def api_send_to_studio():
        b = request.get_json() or {}
        try:
            s = need()
            meta_in = b.get("meta") or {}
            name = st.safe_name(meta_in.get("name") or "STRATEGY_TREE")
            tree, issues = st.to_studio(b["tree"], dict(meta_in, score_col=s["cfg"].get("score_col")))
            if issues and not b.get("force"):
                return ok(status="needs_confirmation", issues=issues)
            meta = dict(name=name, strategy_id=meta_in.get("strategy_id") or "IDX-TB-%04d" % (sum(map(ord, name)) % 10000),
                        status=meta_in.get("status") or "DRAFT", version=1,
                        description=f"Converted from Strategy Tree Builder ({s['cfg']['dataset']})")
            doc = dict(meta=meta, tree=tree, settings=dict(margin=s["cfg"].get("margin"), loss=s["cfg"].get("loss"),
                                                           baseline_cutoff=None),
                       saved_at=st.now(), saved_by=_browser_user(),
                       history=[dict(version=1, saved_at=st.now(), saved_by=_browser_user(), note="Strategy Tree Builder")])
            _write_json(_folder(STRATEGY_FOLDER), f"/strategies/{name}.json", doc)
            return ok(status="ok", path=f"/strategies/{name}.json", issues=issues)
        except Exception as e:
            return fail(e)

    @app.route("/api/write_dataset", methods=["POST"])
    def api_write_dataset():
        b = request.get_json() or {}
        try:
            with _lock:
                s = need()
                res, masks, _ = st.evaluate_full(s["df"], b["tree"], s["cfg"].get("score_col"), s["y"], s["cfg"]["dataset"])
                res, _ = st.predict(res, b["tree"], masks, s["y"], s["cfg"]["dataset"])
                out = pd.concat([s["df"], res], axis=1)
                import binning_output
                name = b.get("output") or f"{s['cfg']['dataset']}_tree"
                info = binning_output.write_output_dataset(s["cfg"]["dataset"], name, out, source_webapp="Strategy_Tree_Builder",
                                                           description=f"Strategy tree {(b.get('meta') or {}).get('name', '')} decisions")
                return ok(dataset=name, rows=len(out), info=info if isinstance(info, dict) else None)
        except Exception as e:
            return fail(e)
