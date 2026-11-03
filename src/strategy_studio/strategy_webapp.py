"""Flask routes for the Strategy Studio webapp.

The webapp's Python tab only needs:

    from strategy_webapp import register
    register(app)

Decision-tree logic lives in ``strategy_engine``; the UI in ``strategy_ui/``.
Strategies, exports and (simulated) deployment receipts are kept in the managed
folder ``decision_strategies``:

    /strategies/<NAME>.json                      editable strategy (tree + settings)
    /exports/<NAME>_v<version>.<target>.<ext>    exported artefacts
    /deployments/<NAME>_v<version>_<ts>.json     simulated deployment receipts
"""

import hashlib
import json
import math
import os
import threading
import traceback
import uuid

import dataiku
import numpy as np
import pandas as pd
from flask import Response, jsonify, request

import strategy_engine as se

FOLDER = "decision_strategies"
UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "strategy_ui")

_lock = threading.RLock()
_pop = {}   # dataset, df, score_col, target, profile, meta


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
    return o


def ok(**payload):
    return jsonify(clean(payload))


def fail(e):
    if isinstance(e, se.StrategyError):
        return jsonify(error=str(e)), 400
    traceback.print_exc()
    return jsonify(error=str(e)), 500


def need_pop():
    if _pop.get("df") is None:
        raise se.StrategyError("Load a scored dataset first.")
    return _pop


def folder():
    return dataiku.Folder(FOLDER)


def _write_json(path, obj):
    with folder().get_writer(path) as w:
        w.write(json.dumps(clean(obj), indent=2).encode("utf-8"))


def _write_text(path, text):
    with folder().get_writer(path) as w:
        w.write(text.encode("utf-8"))


def _read_json(path):
    with folder().get_download_stream(path) as s:
        return json.loads(s.read().decode("utf-8"))


def _browser_user():
    try:
        return dataiku.api_client().get_auth_info_from_browser_headers(dict(request.headers)).get("authIdentifier", "")
    except Exception:
        return ""


def _project_key():
    try:
        return dataiku.default_project_key()
    except Exception:
        return ""


def _settings(b):
    """Economics / baseline from the request, falling back to the loaded population."""
    p = need_pop()
    def f(k, default):
        v = b.get(k, default)
        return None if v in (None, "") else float(v)
    return dict(margin=f("margin", p["meta"].get("margin")), loss=f("loss", p["meta"].get("loss")),
                baseline_cutoff=f("baseline_cutoff", p["meta"].get("cutoff")))


def _meta(b):
    p = need_pop()
    m = dict(b.get("meta") or {})
    m.setdefault("score_col", p["score_col"])
    m.setdefault("dataset", p["dataset"])
    m.setdefault("project", _project_key())
    m.setdefault("baseline_cutoff", p["meta"].get("cutoff"))
    if not m.get("author"):
        m["author"] = _browser_user()
    return m


def _summary(bt):
    t, base = bt["total"], bt.get("baseline") or {}
    keys = ("n", "approval_rate", "refer_rate", "decline_rate", "bad_rate_approved", "profit")
    return {"strategy": {k: t.get(k) for k in keys},
            "baselineScoreCutoff": {k: base.get(k) for k in keys + ("cutoff",)} if base else None}


def _column_types():
    return {c["name"]: c["kind"] for c in need_pop()["profile"]}


def register(app):

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
            project = dataiku.api_client().get_default_project()
            return ok(datasets=sorted(d["name"] for d in project.list_datasets()))
        except Exception as e:
            return fail(e)

    @app.route("/api/dataset_info")
    def api_dataset_info():
        """Columns + scorecard metadata (written by Scorecard_Scalling_Opt) before loading."""
        try:
            name = request.args["dataset"]
            cols = [c["name"] for c in dataiku.Dataset(name).read_schema()]
            kv = {}
            try:
                raw = dataiku.api_client().get_default_project().get_dataset(name).get_settings().get_raw()
                kv = (raw.get("customMeta") or {}).get("kv") or {}
            except Exception:
                traceback.print_exc()
            return ok(columns=cols, meta=kv)
        except Exception as e:
            return fail(e)

    @app.route("/api/load", methods=["POST"])
    def api_load():
        b = request.get_json() or {}
        try:
            with _lock:
                name = b["dataset"]
                df = dataiku.Dataset(name).get_dataframe()
                score_col, target = b.get("score_col"), b.get("target") or None
                if score_col not in df.columns:
                    raise se.StrategyError(f"Score column '{score_col}' not found.")
                if target and target not in df.columns:
                    raise se.StrategyError(f"Target '{target}' not found.")
                kv = b.get("meta") or {}
                meta = dict(margin=kv.get("avg_margin_per_good"), loss=kv.get("avg_loss_per_bad"),
                            cutoff=kv.get("scorecard_cutoff"))
                s = pd.to_numeric(df[score_col], errors="coerce")
                if meta["cutoff"] is None:
                    meta["cutoff"] = float(round(s.median()))
                _pop.clear()
                _pop.update(dataset=name, df=df, score_col=score_col, target=target,
                            profile=se.column_profile(df), meta=meta)
                y = se._target(df, target)
                return ok(dataset=name, n_rows=len(df), score_col=score_col, target=target,
                          score_min=float(s.min()), score_max=float(s.max()),
                          bad_rate=float(np.nanmean(y)) if y is not None and np.any(~np.isnan(y)) else None,
                          columns=_pop["profile"], meta=meta)
        except Exception as e:
            return fail(e)

    @app.route("/api/template", methods=["POST"])
    def api_template():
        b = request.get_json() or {}
        try:
            p = need_pop()
            cutoff = b.get("cutoff") if b.get("cutoff") not in (None, "") else p["meta"].get("cutoff")
            return ok(tree=se.template(b.get("name", "cutoff_only"), p["score_col"], cutoff, p["df"].columns))
        except Exception as e:
            return fail(e)

    @app.route("/api/evaluate", methods=["POST"])
    def api_evaluate():
        b = request.get_json() or {}
        try:
            with _lock:
                p = need_pop()
                tree = b["tree"]
                nums = [c["name"] for c in p["profile"] if c["kind"] == "numeric"]
                issues = se.validate(tree, p["df"].columns, nums)
                if any(i["level"] == "error" for i in issues):
                    return ok(issues=issues, valid=False)
                st = _settings(b)
                bt, _ = se.backtest(p["df"], tree, p["score_col"], p["target"], st["margin"], st["loss"],
                                    st["baseline_cutoff"])
                return ok(issues=issues, valid=True, settings=st, **bt)
        except Exception as e:
            return fail(e)

    @app.route("/api/export", methods=["POST"])
    def api_export():
        b = request.get_json() or {}
        try:
            with _lock:
                p = need_pop()
                fmt = b.get("format", "interconnect")
                tree, meta = b["tree"], _meta(b)
                summary = None
                if fmt in ("interconnect", "powercurve", "oscilar", "pega", "provenir", "taktile") and not any(i["level"] == "error" for i in se.validate(tree, p["df"].columns)):
                    st = _settings(b)
                    bt, _ = se.backtest(p["df"], tree, p["score_col"], p["target"], st["margin"], st["loss"],
                                        st["baseline_cutoff"])
                    summary = _summary(bt)
                text = se.export(fmt, tree, meta, _column_types(), summary)
                ext = se.EXPORT_FORMATS[fmt][1]
                return ok(format=fmt, content=text, extension=ext, label=se.EXPORT_FORMATS[fmt][0])
        except Exception as e:
            return fail(e)

    @app.route("/api/import", methods=["POST"])
    def api_import():
        """Advanced JSON: accept a generic export, an InterConnect export or a bare tree."""
        try:
            return ok(tree=se.tree_from_any(request.get_json()))
        except Exception as e:
            return fail(e)

    # ── strategies in the managed folder ────────────────────────────

    @app.route("/api/strategy/list")
    def api_strategy_list():
        try:
            items = []
            for path in folder().list_paths_in_partition():
                if path.startswith("/strategies/") and path.endswith(".json"):
                    try:
                        s = _read_json(path)
                        m = s.get("meta", {})
                        items.append(dict(file=path, name=m.get("name"), strategy_id=m.get("strategy_id"),
                                          version=m.get("version"), status=m.get("status"),
                                          saved_at=s.get("saved_at"), saved_by=s.get("saved_by"),
                                          n_leaves=len(se.leaves(s["tree"]))))
                    except Exception:
                        traceback.print_exc()
            items.sort(key=lambda i: i.get("saved_at") or "", reverse=True)
            return ok(strategies=items, folder=FOLDER)
        except Exception as e:
            return jsonify(error=f"Cannot read managed folder '{FOLDER}': {e}"), 500

    @app.route("/api/strategy/save", methods=["POST"])
    def api_strategy_save():
        b = request.get_json() or {}
        try:
            meta = dict(b.get("meta") or {})
            name = se.safe_name(meta.get("name"))
            if not name:
                raise se.StrategyError("Give the strategy a name.")
            meta["name"] = name
            path = "/strategies/%s.json" % name
            prev = None
            if path in folder().list_paths_in_partition():
                prev = _read_json(path)
            if b.get("new_version") and prev:
                meta["version"] = int(prev.get("meta", {}).get("version") or 1) + 1
            meta.setdefault("version", 1)
            doc = dict(meta=meta, tree=b["tree"], settings=b.get("settings") or {},
                       saved_at=se._now(), saved_by=_browser_user(),
                       history=((prev or {}).get("history") or []) + [dict(version=meta["version"], saved_at=se._now(),
                                                                         saved_by=_browser_user(), note=b.get("note", ""))])
            _write_json(path, doc)
            return ok(status="ok", file=path, meta=meta)
        except Exception as e:
            return fail(e)

    @app.route("/api/strategy/load", methods=["POST"])
    def api_strategy_load():
        try:
            return ok(**_read_json((request.get_json() or {})["file"]))
        except Exception as e:
            return fail(e)

    @app.route("/api/compare", methods=["POST"])
    def api_compare():
        b = request.get_json() or {}
        try:
            with _lock:
                p = need_pop()
                def tree_of(x):
                    return _read_json(x)["tree"] if isinstance(x, str) else x
                st = _settings(b)
                return ok(**se.compare(p["df"], tree_of(b["champion"]), tree_of(b["challenger"]), p["score_col"],
                                       p["target"], st["margin"], st["loss"]))
        except Exception as e:
            return fail(e)

    # ── export / simulated deployment ───────────────────────────────

    @app.route("/api/save_export", methods=["POST"])
    def api_save_export():
        b = request.get_json() or {}
        try:
            with _lock:
                meta = _meta(b)
                name = se.safe_name(b.get("file_name") or meta.get("name"))
                fmt = b.get("format", "interconnect")
                text = b.get("content")
                if not text:
                    raise se.StrategyError("Nothing to save.")
                path = "/exports/%s_v%s.%s.%s" % (name, meta.get("version", 1), fmt, se.EXPORT_FORMATS[fmt][1])
                _write_text(path, text)
                return ok(status="ok", file=path, folder=FOLDER)
        except Exception as e:
            return fail(e)

    @app.route("/api/deploy", methods=["POST"])
    def api_deploy():
        """SIMULATED deployment: stores the package and a receipt; nothing leaves Dataiku."""
        b = request.get_json() or {}
        try:
            with _lock:
                p = need_pop()
                meta, tree = _meta(b), b["tree"]
                errors = [i for i in se.validate(tree, p["df"].columns) if i["level"] == "error"]
                if errors:
                    raise se.StrategyError("Fix validation errors before deploying: " + errors[0]["message"])
                st = _settings(b)
                bt, _ = se.backtest(p["df"], tree, p["score_col"], p["target"], st["margin"], st["loss"],
                                    st["baseline_cutoff"])
                package = se.to_interconnect(tree, meta, _column_types(), _summary(bt))
                name = se.safe_name(b.get("file_name") or meta.get("name"))
                ts = se._now()
                pkg_path = "/exports/%s_v%s.interconnect.json" % (name, meta.get("version", 1))
                _write_json(pkg_path, package)
                receipt = dict(
                    status="SIMULATED", target="Equifax InterConnect", environment=b.get("environment", "UAT"),
                    deployment_id="DEP-" + uuid.uuid4().hex[:10].upper(), strategy_id=meta.get("strategy_id"),
                    strategy_name=name, version=meta.get("version", 1), deployed_at=ts, deployed_by=_browser_user(),
                    package=pkg_path, checksum=package["governance"]["checksum"],
                    note="Simulated deployment for demonstration: the package was validated and stored in the "
                         "decision_strategies folder; no call was made to an external system.",
                    kpis=_summary(bt)["strategy"])
                rpath = "/deployments/%s_v%s_%s.json" % (name, meta.get("version", 1), ts.replace(":", "").replace("-", ""))
                _write_json(rpath, receipt)
                receipt["file"] = rpath
                return ok(**receipt)
        except Exception as e:
            return fail(e)

    @app.route("/api/deployments")
    def api_deployments():
        try:
            out = []
            for path in folder().list_paths_in_partition():
                if path.startswith("/deployments/") and path.endswith(".json"):
                    try:
                        out.append(_read_json(path))
                    except Exception:
                        traceback.print_exc()
            out.sort(key=lambda d: d.get("deployed_at") or "", reverse=True)
            return ok(deployments=out)
        except Exception as e:
            return fail(e)

    @app.route("/api/write_dataset", methods=["POST"])
    def api_write_dataset():
        b = request.get_json() or {}
        try:
            with _lock:
                p = need_pop()
                tree = b["tree"]
                res, _ = se.evaluate(p["df"], tree, p["score_col"])
                out = pd.concat([p["df"], res], axis=1)
                name = (b.get("name") or "").strip() or "%s_strategy" % p["dataset"]
                meta = _meta(b)
                from binning_output import write_output_dataset
                info = write_output_dataset(
                    p["dataset"], name, out, source_webapp="Strategy Studio",
                    description="Decisions of strategy %s v%s (%s) on %s, written by Strategy Studio."
                                % (meta.get("name"), meta.get("version", 1), meta.get("strategy_id"), p["dataset"]))
                counts = res["strategy_decision"].value_counts().to_dict()
                return ok(status="ok", dataset=name, n_rows=len(out), decisions=counts, **info)
        except Exception as e:
            return fail(e)
