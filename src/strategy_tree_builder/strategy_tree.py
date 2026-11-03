"""
strategy_tree.py
================
N-ary strategy trees for Strategy Tree Builder: Strategy Studio's decisioning combined with
optimal binning (optbinning) for the splits.

Node
----
  {"id", "name",
   "calcs": [{"name", "expr"}],          business-logic calculations for rows reaching the node,
                                          usable by every descendant (splits, leaves, metrics)
   "split": null | split,                 null = leaf
   "children": [node, ...],               one per branch
   "leaf": {"mode": "approve" | "refer" | "decline" | "cutoff" | "none" | "output",
            "score_expr", "approve_at", "refer_at",        cutoff: score formula vs cut-offs
            "decision_output",                            output: name of the output holding the decision
            "reason_codes": [..], "pricing_tier", "limit_expr",
            "outputs": [{"name", "expr"}]}}               extra leaf outputs (formulas)
  The root may carry "default_outputs": [{"name", "expr"}], evaluated in every leaf unless the
  leaf defines an output with the same name. Leaf formulas can read leaf_id, leaf_event_rate,
  leaf_event_rate_raw, leaf_n_known, leaf_score, leaf_decision and the leaf's earlier outputs.
  Order in a leaf: score -> decision (cut-off / fixed) -> leaf outputs -> default outputs ->
  credit-limit formula; with a decision from an output, leaf_decision exists from that output on.

Split kinds (any number of branches per node, any variable – including a calculation):
  numeric      {"kind": "numeric", "variable", "cuts": [c1..ck]}        k+1 branches:
               x < c1 | c1 <= x < c2 | ... | x >= ck   (optbinning convention)
  categorical  {"kind": "categorical", "variable", "groups": [[..], ..], "other_branch"}
  rule         {"kind": "rule", "rules": [expr, ..]}                    first true rule wins,
               last branch = otherwise
  + "missing_branch": index of the branch missing values follow, or "missing_separate": true
    for an extra last branch "Missing" (numeric / categorical);
  + "source": manual | optbinning | binning_config:<name> | visual_ml.

Everything is pure pandas / numpy (optbinning optional); the webapp and agents share it.
"""

import copy
import datetime
import hashlib
import json
import math
import re

import numpy as np
import pandas as pd

import tree_expr as tx

LEAF_MODES = ("approve", "refer", "decline", "cutoff", "none", "output")
# Leaf values usable in leaf formulas (score formula: the first four only).
LEAF_CONTEXT = ("leaf_id", "leaf_event_rate", "leaf_event_rate_raw", "leaf_n_known", "leaf_score", "leaf_decision")
LEAF_RATE_NAMES = ("leaf_event_rate", "leaf_event_rate_raw", "leaf_n_known")
DECISIONS = ("APPROVE", "REFER", "DECLINE")
MISS_CODE, OTHER_CODE, K = 31, 30, 32
DEFAULT_METRICS = ["n", "share", "bad_rate", "gini", "entropy", "dominant", "split"]
METRIC_LABELS = {
    "n": "Samples", "share": "% of population", "n_known": "Known outcome", "bads": "Events",
    "bad_rate": "Event rate", "good_rate": "Non-event rate", "gini": "Gini impurity", "entropy": "Entropy",
    "dominant": "Dominant class prob.", "avg_score": "Avg score", "approval_rate": "Approval rate",
    "split": "Split values", "split_iv": "Split IV", "split_gain": "Split info gain %", "profit": "Profit",
    "pd": "Predicted event rate",
}


class TreeError(ValueError):
    pass


# ════════════════════════════════════════════════════════════════════
# structure helpers
# ════════════════════════════════════════════════════════════════════

def walk(node, depth=0, path=()):
    """Yield (node, depth, path); path = tuple of (ancestor, branch index)."""
    yield node, depth, path
    for i, c in enumerate(node.get("children") or []):
        yield from walk(c, depth + 1, path + ((node, i),))


def leaves(tree):
    return [n for n, _, _ in walk(tree) if not n.get("split")]


def find(tree, node_id):
    for n, _, p in walk(tree):
        if n["id"] == node_id:
            return n, p
    raise TreeError(f"Node {node_id} not found.")


def next_id(tree):
    nums = [int(m.group(1)) for n, _, _ in walk(tree) for m in [re.match(r"n(\d+)$", str(n["id"]))] if m]
    return f"n{max(nums, default=0) + 1}"


def new_leaf(node_id, name="", mode="cutoff", score_expr="", approve_at=None, refer_at=None,
             reason_codes=None, tier="", limit_expr=""):
    return {"id": node_id, "name": name, "calcs": [], "split": None, "children": [],
            "leaf": {"mode": mode, "score_expr": score_expr, "approve_at": approve_at, "refer_at": refer_at,
                     "reason_codes": list(reason_codes or []), "pricing_tier": tier, "limit_expr": limit_expr,
                     "outputs": []}}


def n_branches(split):
    k = split["kind"]
    if k == "numeric":
        n = len(split.get("cuts") or []) + 1
    elif k == "categorical":
        n = len(split.get("groups") or []) + (1 if split.get("other_separate") else 0)
    else:
        return len(split.get("rules") or []) + 1
    return n + (1 if split.get("missing_separate") else 0)


def _fmt(v):
    f = _num(v)
    if f is None:
        return str(v)
    return f"{f:,.4g}" if abs(f) < 1e4 else f"{f:,.0f}"


def _num(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        f = float(v)
        return None if math.isnan(f) else f
    except (TypeError, ValueError):
        return None


def branch_labels(split):
    k, out = split["kind"], []
    if k == "numeric":
        c = split.get("cuts") or []
        for i in range(len(c) + 1):
            if not c:
                out.append("all")
            elif i == 0:
                out.append(f"< {_fmt(c[0])}")
            elif i == len(c):
                out.append(f"≥ {_fmt(c[-1])}")
            else:
                out.append(f"[{_fmt(c[i - 1])}, {_fmt(c[i])})")
    elif k == "categorical":
        for g in split.get("groups") or []:
            out.append(", ".join(str(x) for x in g[:4]) + (f" +{len(g) - 4}" if len(g) > 4 else ""))
        if split.get("other_separate"):
            out.append("Other")
    else:
        out = [str(r) for r in split.get("rules") or []] + ["Otherwise"]
        return out
    if split.get("missing_separate"):
        out.append("Missing")
    elif split.get("missing_branch") is not None and 0 <= int(split["missing_branch"]) < len(out):
        out[int(split["missing_branch"])] += " + missing"
    return out


def describe_split(split):
    if not split:
        return ""
    if split["kind"] == "rule":
        return f"{len(split.get('rules') or [])} rule(s)"
    return f"{split['variable']} → {n_branches(split)} branches"


# ── labels: a classification assigned by clicking nodes (risk rating, pricing tier, segment...) ──
# tree["label_defs"] = [{"name": "Rating", "values": ["AAA", "AA", ...]}]; node["labels"] = {"Rating": "AA"}.
# A node's label applies to every row below it unless a deeper node sets its own; each label type
# becomes a column label_<name> and can be read in leaf formulas as label_<name>.
LABEL_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
DEFAULT_LABEL_DEFS = [{"name": "Rating", "values": ["AAA", "AA", "A", "BBB", "BB", "B"]}]


def resolve_labels(tree):
    """{node_id: {label: (value, source node id)}} after inheritance."""
    out = {}

    def go(n, inherited):
        cur = dict(inherited)
        for k, v in (n.get("labels") or {}).items():
            if v not in (None, ""):
                cur[k] = (str(v), n["id"])
        out[n["id"]] = cur
        for c in n.get("children") or []:
            go(c, cur)

    go(tree, {})
    return out


def label_names(tree):
    """Label types in use: the defined ones first, then any other name set on a node."""
    names = [d["name"] for d in tree.get("label_defs") or [] if d.get("name")]
    for n, _, _ in walk(tree):
        for k, v in (n.get("labels") or {}).items():
            if v not in (None, "") and k not in names:
                names.append(k)
    return names


def leaf_outputs(tree, leaf):
    """Outputs of a leaf, in evaluation order: the leaf's own outputs first, then the root's
    default outputs it does not override (so defaults such as pricing can read the leaf's decision)."""
    own = [o for o in (leaf.get("outputs") or []) if o.get("name")]
    names_ = {o["name"] for o in own}
    return own + [o for o in (tree.get("default_outputs") or []) if o.get("name") and o["name"] not in names_]


def _as_decision(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "REFER"
    return str(v).strip().upper()


def describe_leaf(leaf):
    m = leaf.get("mode", "cutoff")
    if m == "none":
        txt = "no decision (segment)"
    elif m == "output":
        txt = f"decision = output {leaf.get('decision_output') or '?'}"
    elif m != "cutoff":
        txt = m.upper()
    else:
        txt = f"{leaf.get('score_expr') or 'score'} ≥ {_fmt(leaf.get('approve_at'))} approve"
        if _num(leaf.get("refer_at")) is not None:
            txt += f", ≥ {_fmt(leaf.get('refer_at'))} refer"
    if leaf.get("pricing_tier"):
        txt += f" · tier {leaf['pricing_tier']}"
    if leaf.get("limit_expr"):
        txt += f" · limit {leaf['limit_expr']}"
    return txt


# ════════════════════════════════════════════════════════════════════
# validation
# ════════════════════════════════════════════════════════════════════

def validate(tree, columns, default_score=None):
    """List of {node_id, level, message}. ``columns``: available dataset columns."""
    issues, ids = [], set()
    cols = set(columns)

    def add(n, level, msg):
        issues.append({"node_id": n.get("id"), "level": level, "message": msg})

    def check_expr(n, expr, known, what):
        try:
            for nm in tx.names(expr):
                if nm not in known:
                    add(n, "error", f"{what}: unknown column or calculation '{nm}'.")
        except tx.ExprError as e:
            add(n, "error", f"{what}: {e}")

    defs = {d.get("name"): set(map(str, d.get("values") or [])) for d in tree.get("label_defs") or []}
    for d in tree.get("label_defs") or []:
        if not LABEL_NAME_RE.match(d.get("name") or ""):
            add(tree, "error", f"Label name '{d.get('name')}' must be letters, digits or _ (e.g. Rating, Pricing_tier).")
    label_vars = {"label_" + k for k in label_names(tree)}

    def rec(n, known):
        if n.get("id") in ids:
            add(n, "error", f"Duplicate node id {n.get('id')}.")
        ids.add(n.get("id"))
        for k, v in (n.get("labels") or {}).items():
            if v not in (None, "") and defs.get(k) and str(v) not in defs[k]:
                add(n, "warning", f"Label {k} = '{v}' is not in the list of values of {k}.")
        known = set(known)
        for c in n.get("calcs") or []:
            if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", c.get("name") or ""):
                add(n, "error", f"Calculation name '{c.get('name')}' must be an identifier.")
            check_expr(n, c.get("expr"), known, f"Calculation {c.get('name')}")
            known.add(c.get("name"))
        s = n.get("split")
        if s:
            kids = n.get("children") or []
            if s["kind"] in ("numeric", "categorical"):
                if s.get("variable") not in known:
                    add(n, "error", f"Split variable '{s.get('variable')}' is not a column or calculation.")
                if s["kind"] == "numeric":
                    c = [_num(x) for x in s.get("cuts") or []]
                    if not c or any(x is None for x in c):
                        add(n, "error", "Numeric split needs at least one numeric cut.")
                    elif c != sorted(c) or len(set(c)) != len(c):
                        add(n, "error", "Cuts must be strictly increasing.")
                else:
                    seen = set()
                    for g in s.get("groups") or []:
                        for v in g:
                            if str(v) in seen:
                                add(n, "error", f"Category '{v}' is in two groups.")
                            seen.add(str(v))
                    if not s.get("groups"):
                        add(n, "error", "Categorical split needs at least one group.")
                    ob = s.get("other_branch")
                    if not s.get("other_separate") and (ob is None or not 0 <= int(ob) < len(s.get("groups") or [])):
                        add(n, "error", "Choose the branch unlisted categories follow.")
                mb = s.get("missing_branch")
                if not s.get("missing_separate") and (mb is None or not 0 <= int(mb) < n_branches(s)):
                    add(n, "error", "Choose the branch missing values follow.")
            elif s["kind"] == "rule":
                if not s.get("rules"):
                    add(n, "error", "Rule split needs at least one rule.")
                for r in s.get("rules") or []:
                    check_expr(n, r, known, "Rule")
            else:
                add(n, "error", f"Unknown split kind '{s['kind']}'.")
            if len(kids) != n_branches(s):
                add(n, "error", f"Split has {n_branches(s)} branches but {len(kids)} children.")
            for k in kids:
                rec(k, known)
        else:
            lf = n.get("leaf") or {}
            mode = lf.get("mode", "cutoff")
            if mode not in LEAF_MODES:
                add(n, "error", f"Unknown leaf decision '{mode}'.")
            score_known = known | {"leaf_id"} | set(LEAF_RATE_NAMES) | label_vars
            if lf.get("score_expr"):
                check_expr(n, lf["score_expr"], score_known, "Score")
            if mode == "cutoff":
                expr = lf.get("score_expr") or default_score
                if not expr:
                    add(n, "error", "Set the score formula for the cut-off.")
                elif not lf.get("score_expr"):
                    check_expr(n, expr, score_known, "Score")
                aa, ra = _num(lf.get("approve_at")), _num(lf.get("refer_at"))
                if aa is None:
                    add(n, "error", "Set the approve cut-off.")
                if aa is not None and ra is not None and ra > aa:
                    add(n, "error", "Refer cut-off must be below the approve cut-off.")
            out_known = score_known | {"leaf_score"} | ({"leaf_decision"} if mode != "output" else set())
            names_ = []
            for o in leaf_outputs(tree, lf):
                if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", o.get("name") or ""):
                    add(n, "error", f"Output name '{o.get('name')}' must be an identifier.")
                check_expr(n, o.get("expr"), out_known, f"Output {o.get('name')}")
                out_known = out_known | {o.get("name")}
                if mode == "output" and o.get("name") == lf.get("decision_output"):
                    out_known = out_known | {"leaf_decision"}
                names_.append(o.get("name"))
            if mode == "output" and lf.get("decision_output") not in names_:
                add(n, "error", f"Decision output '{lf.get('decision_output') or ''}' is not an output of this leaf.")
            if lf.get("limit_expr"):
                check_expr(n, lf["limit_expr"], out_known | {"leaf_decision"}, "Limit")
            if mode in ("decline", "refer") and not lf.get("reason_codes"):
                add(n, "warning", "Declines and referrals usually carry a reason code.")

    rec(tree, cols)
    return issues


# ════════════════════════════════════════════════════════════════════
# evaluation
# ════════════════════════════════════════════════════════════════════

class Env(object):
    """Name lookup for formulas: calculations first, then dataset columns."""

    def __init__(self, df):
        self.df, self.calcs, self.ctx = df, {}, {}

    def __contains__(self, k):
        return k in self.ctx or k in self.calcs or k in self.df.columns

    def __getitem__(self, k):
        if k in self.ctx:
            return self.ctx[k]
        return self.calcs[k] if k in self.calcs else self.df[k]


def _canon(z):
    return tx._canon(z)


def route(split, env, index):
    """Branch index (int array) per row for one split."""
    n = len(index)
    k = split["kind"]
    if k == "rule":
        out = np.full(n, len(split["rules"]), dtype=int)
        done = np.zeros(n, dtype=bool)
        for i, r in enumerate(split["rules"]):
            m = tx._bool(tx.evaluate(r, env, index)).fillna(False).values.astype(bool) & ~done
            out[m] = i
            done |= m
        return out
    col = env[split["variable"]]
    missing = col.isna().values
    nb = n_branches(split)
    miss_to = nb - 1 if split.get("missing_separate") else int(split.get("missing_branch") or 0)
    if k == "numeric":
        x = pd.to_numeric(col, errors="coerce").values.astype(float)
        cuts = np.array([float(c) for c in split["cuts"]])
        out = np.searchsorted(cuts, np.nan_to_num(x, nan=0.0), side="right")
        missing = missing | np.isnan(x)
    else:
        groups = split.get("groups") or []
        lookup = {_canon(v): i for i, g in enumerate(groups) for v in g}
        other = len(groups) if split.get("other_separate") else int(split.get("other_branch") or 0)
        out = col.astype(object).map(lambda z: lookup.get(_canon(z), other)).values.astype(int)
    out = np.where(missing, miss_to, out)
    return out


def evaluate(df, tree, default_score=None, leaf_rates=None):
    """Route every row. Returns (result DataFrame, {node_id: mask}, env).
    ``leaf_rates``: {leaf_id: {pd, raw, n_known}} exposed to leaf formulas as leaf_event_rate..."""
    n, idx = len(df), df.index
    env = Env(df)
    leaf_id = np.empty(n, dtype=object)
    path = np.empty(n, dtype=object)
    score = np.full(n, np.nan)
    decision = np.empty(n, dtype=object)
    reasons = np.empty(n, dtype=object)
    tier = np.empty(n, dtype=object)
    limit = np.full(n, np.nan)
    outputs = {}
    masks = {}
    resolved = resolve_labels(tree)
    lnames = label_names(tree)
    label_cols = {k: np.full(n, None, dtype=object) for k in lnames}

    def go(node, mask, trail):
        masks[node["id"]] = mask
        for c in node.get("calcs") or []:
            val = tx.evaluate(c["expr"], env, idx)
            prev = env.calcs.get(c["name"])
            env.calcs[c["name"]] = val.where(mask, prev) if prev is not None else val.where(mask)
        s = node.get("split")
        if s:
            if not mask.any():
                for ch in node["children"]:
                    go(ch, mask, trail)
                return
            br = route(s, env, idx)
            labels = branch_labels(s)
            for i, ch in enumerate(node["children"]):
                lab = (s.get("variable") + " " if s["kind"] != "rule" else "") + labels[i]
                go(ch, mask & (br == i), trail + [lab])
            return
        ii = np.where(mask)[0]
        lf = node.get("leaf") or {}
        mode = lf.get("mode", "cutoff")
        rt = (leaf_rates or {}).get(node["id"]) or {}
        const = lambda v: pd.Series(np.nan if v is None else v, index=idx, dtype=float)
        ctx = env.ctx
        ctx.clear()
        for k in lnames:
            v = (resolved.get(node["id"], {}).get(k) or (None,))[0]
            label_cols[k][ii] = v
            ctx["label_" + k] = pd.Series(v, index=idx, dtype=object)
        ctx.update(leaf_id=pd.Series(node["id"], index=idx, dtype=object), leaf_event_rate=const(rt.get("pd")),
                   leaf_event_rate_raw=const(rt.get("raw")), leaf_n_known=const(rt.get("n_known")))
        expr = lf.get("score_expr") or default_score
        if expr and len(ii):
            sc = pd.to_numeric(tx.evaluate(expr, env, idx), errors="coerce").values[ii]
            score[ii] = sc
        else:
            sc = np.full(len(ii), np.nan)
        ctx["leaf_score"] = pd.Series(score, index=idx)
        if mode == "cutoff":
            aa, ra = _num(lf.get("approve_at")), _num(lf.get("refer_at"))
            with np.errstate(invalid="ignore"):
                dec = np.where(sc >= (aa if aa is not None else np.inf), "APPROVE",
                               np.where(sc >= (ra if ra is not None else np.inf), "REFER", "DECLINE"))
            dec = np.where(np.isnan(sc), "REFER", dec)  # no score -> manual review
        elif mode in ("none", "output"):
            dec = np.full(len(ii), None, dtype=object)
        else:
            dec = np.full(len(ii), mode.upper())
        decision[ii] = dec
        if mode != "output":
            ctx["leaf_decision"] = pd.Series(decision, index=idx)
        for o in leaf_outputs(tree, lf):
            if not len(ii):
                continue
            val = tx.evaluate(o["expr"], env, idx)
            val = val if isinstance(val, pd.Series) else pd.Series(val, index=idx)
            arr = outputs.setdefault(o["name"], np.full(n, None, dtype=object))
            arr[ii] = val.values[ii]
            ctx[o["name"]] = val
            if mode == "output" and o["name"] == lf.get("decision_output"):
                decision[ii] = [_as_decision(v) for v in arr[ii]]
                ctx["leaf_decision"] = pd.Series(decision, index=idx)
        if mode == "output" and len(ii) and lf.get("decision_output") not in outputs:
            decision[ii] = "REFER"
        leaf_id[ii] = node["id"]
        path[ii] = " > ".join(trail + [node.get("name") or node["id"]])
        reasons[ii] = ";".join(lf.get("reason_codes") or [])
        tier[ii] = lf.get("pricing_tier") or ""
        if lf.get("limit_expr") and len(ii):
            limit[ii] = pd.to_numeric(tx.evaluate(lf["limit_expr"], env, idx), errors="coerce").values[ii]
        else:
            limit[ii] = 1.0
        ctx.clear()

    go(tree, np.ones(n, dtype=bool), [])
    res = pd.DataFrame({"tree_leaf": leaf_id, "tree_path": path, "tree_score": score, "tree_decision": decision,
                        "reason_codes": reasons, "pricing_tier": tier, "limit_multiplier": limit}, index=idx)
    for k, v in label_cols.items():
        res["label_" + k] = v
    for k, v in outputs.items():
        res["out_" + k] = v
    for k, v in env.calcs.items():
        res["calc_" + k] = v.values
    return res, masks, env


# ════════════════════════════════════════════════════════════════════
# node metrics
# ════════════════════════════════════════════════════════════════════

def target_array(df, target):
    if not target or target not in df.columns:
        return None
    y = np.array(pd.to_numeric(df[target], errors="coerce"), dtype=float)
    y[~np.isin(y, [0.0, 1.0])] = np.nan
    return y


def _entropy(p):
    if p is None or p <= 0 or p >= 1:
        return 0.0
    return float(-(p * math.log2(p) + (1 - p) * math.log2(1 - p)))


def _iv_gain(cnt, bad):
    """IV (0.5 smoothing) and information gain of a partition. cnt / bad: per-branch arrays."""
    cnt, bad = np.asarray(cnt, float), np.asarray(bad, float)
    good = cnt - bad
    B, G, N = bad.sum(), good.sum(), cnt.sum()
    if N == 0 or B == 0 or G == 0:
        return 0.0, 0.0
    use = cnt > 0
    pb = (bad[use] + 0.5) / (B + 0.5 * use.sum())
    pg = (good[use] + 0.5) / (G + 0.5 * use.sum())
    iv = float(np.sum((pg - pb) * np.log(pg / pb)))
    h0 = _entropy(B / N)
    h = sum(c / N * _entropy(b / c) for c, b in zip(cnt[use], bad[use]))
    return iv, (h0 - h) / h0 if h0 > 0 else 0.0


def custom_metric_values(metric, env, masks, index):
    """metric: {name, agg: mean|sum|min|max|median|std|rate|count, expr}."""
    v = tx.evaluate(metric["expr"], env, index)
    agg = metric.get("agg", "mean")
    if agg in ("rate", "count"):
        v = tx._bool(v).astype(float)
    else:
        v = pd.to_numeric(v, errors="coerce")
    vals = v.values.astype(float)
    out = {}
    for nid, m in masks.items():
        x = vals[m]
        x = x[~np.isnan(x)]
        if not len(x):
            out[nid] = None
            continue
        out[nid] = float({"mean": np.mean, "rate": np.mean, "sum": np.sum, "count": np.sum, "min": np.min,
                          "max": np.max, "median": np.median, "std": np.std}[agg](x))
    return out


def node_metrics(df, tree, res, masks, env, target=None, margin=None, loss=None, custom_metrics=None):
    y = target_array(df, target)
    n = len(df)
    dec = res["tree_decision"].values
    sc = res["tree_score"].values
    out = {}
    for node, depth, _ in walk(tree):
        m = masks.get(node["id"], np.zeros(n, dtype=bool))
        k = int(m.sum())
        r = dict(n=k, share=k / n if n else None, depth=depth)
        if y is not None:
            kn = m & ~np.isnan(y)
            nk, b = int(kn.sum()), float(np.nansum(y[kn]))
            p = b / nk if nk else None
            r.update(n_known=nk, bads=int(b), bad_rate=p, good_rate=None if p is None else 1 - p,
                     gini=None if p is None else 2 * p * (1 - p), entropy=None if p is None else _entropy(p),
                     dominant=None if p is None else max(p, 1 - p),
                     dominant_class=None if p is None else ("bad" if p > 0.5 else "good"))
            if node.get("split") and nk:
                br = [masks.get(c["id"], np.zeros(n, dtype=bool)) & kn for c in node["children"]]
                iv, g = _iv_gain([x.sum() for x in br], [np.nansum(y[x]) for x in br])
                r.update(split_iv=iv, split_gain=g)
        app = m & (dec == "APPROVE")
        r["approval_rate"] = float(app.sum() / k) if k else None
        s = sc[m]
        r["avg_score"] = float(np.nanmean(s)) if k and not np.all(np.isnan(s)) else None
        if y is not None and margin is not None and loss is not None:
            ka = app & ~np.isnan(y)
            bads = float(np.nansum(y[ka]))
            r["profit"] = margin * (ka.sum() - bads) - loss * bads
        if node.get("split"):
            r["split"] = branch_labels(node["split"])
        out[node["id"]] = r
    for cm in custom_metrics or []:
        try:
            vals = custom_metric_values(cm, env, masks, df.index)
        except tx.ExprError as e:
            vals = {nid: None for nid in masks}
            for v in out.values():
                v.setdefault("errors", []).append(f"{cm.get('name')}: {e}")
        for nid, v in vals.items():
            out[nid]["cm_" + cm["name"]] = v
    return out


def summary(res, df, target=None, margin=None, loss=None):
    y = target_array(df, target)
    d = res["tree_decision"].values
    n = len(d)
    app = d == "APPROVE"
    out = dict(n=n, approval_rate=float(app.mean()) if n else None, refer_rate=float((d == "REFER").mean()) if n else None,
               decline_rate=float((d == "DECLINE").mean()) if n else None, leaves=int(res["tree_leaf"].nunique()),
               no_decision_rate=float(pd.isna(pd.Series(d)).mean()) if n else None)
    if y is not None:
        ka = app & ~np.isnan(y)
        bads = float(np.nansum(y[ka]))
        out["bad_rate_approved"] = bads / ka.sum() if ka.sum() else None
        if margin is not None and loss is not None:
            out["profit"] = margin * (ka.sum() - bads) - loss * bads
    return out


# ════════════════════════════════════════════════════════════════════
# leaf event-rate prediction (like a Visual ML scoring: prediction / proba per row)
# ════════════════════════════════════════════════════════════════════
#
# Each leaf's observed event rate (target = 1: default, repayment, fraud, ...) becomes the
# predicted event rate (tree_proba_1 / tree_event_rate) of every row reaching it. Small
# leaves are shrunk toward their parent node (credibility weighting, applied top-down):
#     pd(node) = (bads + m * pd(parent)) / (n_known + m)        m = 0 -> raw bad rate
# with a 95 % Wilson interval on the raw rate. Freezing stores the leaf rates in the tree
# (tree["pd_model"]) so new populations – without an outcome – are scored with the
# development-sample rates; the tree signature tells when the structure changed since.

PD_DEFAULTS = {"credibility": 30, "threshold": None, "holdout_pct": 0, "seed": 42}
PD_COLUMNS = ["tree_proba_1", "tree_proba_0", "tree_prediction", "tree_event_rate", "tree_event_rate_raw",
              "tree_event_rate_lower", "tree_event_rate_upper", "tree_event_n_known", "tree_event_rate_source"]


def pd_settings(tree):
    out = dict(PD_DEFAULTS)
    out.update({k: v for k, v in (tree.get("pd_settings") or {}).items() if v not in (None, "")})
    return out


def tree_signature(tree):
    """Hash of everything that decides which leaf a row reaches (splits, calcs, ids)."""
    def strip(n):
        return {"id": n["id"], "calcs": n.get("calcs") or [], "split": n.get("split"),
                "children": [strip(c) for c in n.get("children") or []]}
    return hashlib.sha256(json.dumps(strip(tree), sort_keys=True, default=str).encode()).hexdigest()[:16]


def holdout_mask(n, pct, seed=42):
    pct = float(pct or 0)
    if pct <= 0:
        return np.zeros(n, dtype=bool)
    return np.random.RandomState(int(seed)).rand(n) < pct / 100.0


def _wilson(b, n, z=1.96):
    if not n:
        return None, None
    p = b / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def node_pd(tree, masks, y, credibility=30, rows=None):
    """{node_id: {n_known, bads, raw, pd, lower, upper}} for every node (leaves are the predictions)."""
    if y is None:
        return {}
    known = ~np.isnan(y)
    if rows is not None:
        known = known & rows
    m = max(float(credibility or 0), 0.0)
    out = {}

    def go(node, parent_pd):
        mk = masks.get(node["id"])
        mk = (mk & known) if mk is not None else np.zeros(len(y), dtype=bool)
        nk, b = int(mk.sum()), float(np.nansum(y[mk]))
        raw = b / nk if nk else None
        if parent_pd is None:
            pdv = raw
        else:
            pdv = (b + m * parent_pd) / (nk + m) if (nk + m) > 0 else parent_pd
        lo, hi = _wilson(b, nk)
        out[node["id"]] = dict(n_known=nk, bads=int(b), raw=raw, pd=pdv, lower=lo, upper=hi)
        for c in node.get("children") or []:
            go(c, pdv)

    go(tree, None)
    return out


def freeze_pd(tree, masks, y, dataset="", target="", user="", settings=None):
    """pd_model to store in tree["pd_model"]: leaf event rates from the development rows (holdout excluded)."""
    if y is None:
        raise TreeError("Choose a target (1 = event) to compute leaf event rates.")
    st_ = dict(pd_settings(tree), **(settings or {}))
    hold = holdout_mask(len(y), st_["holdout_pct"], st_["seed"])
    stats = node_pd(tree, masks, y, st_["credibility"], rows=~hold)
    leaf_ids = [lf["id"] for lf in leaves(tree)]
    base = stats.get(tree["id"], {}).get("raw")
    thr = st_["threshold"] if st_["threshold"] not in (None, "") else base
    return {"status": "frozen", "frozen_at": now(), "frozen_by": user, "dataset": dataset, "target": target,
            "n_known": int(stats.get(tree["id"], {}).get("n_known") or 0), "base_rate": base,
            "credibility": st_["credibility"], "threshold": thr, "holdout_pct": st_["holdout_pct"], "seed": st_["seed"],
            "signature": tree_signature(tree), "leaves": {k: stats[k] for k in leaf_ids if k in stats}}


def pd_state(tree):
    """'frozen' | 'stale' (tree changed since freezing) | 'live' (not frozen)."""
    pm = tree.get("pd_model")
    if not pm:
        return "live"
    return "frozen" if pm.get("signature") == tree_signature(tree) else "stale"


def rate_table(tree, masks=None, y=None, dataset=""):
    """Leaf event rates in use: (table {leaf_id: stats}, state, source, threshold).
    Frozen rates when the tree still matches; otherwise live in-sample rates from ``y``."""
    state = pd_state(tree)
    stg = pd_settings(tree)
    if state == "frozen":
        pm = tree["pd_model"]
        return pm["leaves"], state, f"frozen:{pm.get('dataset', '')}@{pm.get('frozen_at', '')}", pm.get("threshold")
    if y is not None and masks is not None:
        hold = holdout_mask(len(y), stg["holdout_pct"], stg["seed"])
        stats = node_pd(tree, masks, y, stg["credibility"], rows=~hold)
        table = {lf["id"]: stats[lf["id"]] for lf in leaves(tree) if lf["id"] in stats}
        thr = stg["threshold"] if stg["threshold"] not in (None, "") else stats.get(tree["id"], {}).get("raw")
        return table, state, f"live in-sample:{dataset}" + (" (stale freeze ignored)" if state == "stale" else ""), thr
    return {}, state, None, None


def uses_leaf_rates(tree):
    """True when a leaf formula reads leaf_event_rate / leaf_event_rate_raw / leaf_n_known."""
    exprs = [o.get("expr") for o in tree.get("default_outputs") or []]
    for lf in leaves(tree):
        L = lf.get("leaf") or {}
        exprs += [L.get("score_expr"), L.get("limit_expr")] + [o.get("expr") for o in L.get("outputs") or []]
    for e in exprs:
        try:
            if e and set(tx.names(e)) & set(LEAF_RATE_NAMES):
                return True
        except tx.ExprError:
            pass
    return False


def evaluate_full(df, tree, default_score=None, y=None, dataset=""):
    """evaluate() with the leaf event rates available to leaf formulas (two passes only when needed:
    routing does not depend on leaf formulas, so the second pass sees the same leaves)."""
    res, masks, env = evaluate(df, tree, default_score)
    if uses_leaf_rates(tree):
        table = rate_table(tree, masks, y, dataset)[0]
        res, masks, env = evaluate(df, tree, default_score, leaf_rates=table)
    return res, masks, env


def predict(res, tree, masks=None, y=None, dataset=""):
    """Add tree_proba_1 / tree_proba_0 / tree_prediction (+ raw, interval, n_known, source) to ``res``.
    Frozen leaf event rates when the tree still matches; otherwise live in-sample rates from ``y``."""
    table, state, source, thr = rate_table(tree, masks, y, dataset)
    if source is None:
        return res, dict(state=state, source=None, threshold=None)
    leaf = res["tree_leaf"]
    get = lambda k: leaf.map(lambda i: (table.get(i) or {}).get(k)).astype(float)
    p1 = get("pd")
    res["tree_proba_1"] = p1
    res["tree_proba_0"] = 1 - p1
    res["tree_prediction"] = np.where(p1.isna(), np.nan, (p1 >= (thr if thr is not None else 0.5)).astype(float))
    res["tree_event_rate"] = p1
    res["tree_event_rate_raw"] = get("raw")
    res["tree_event_rate_lower"] = get("lower")
    res["tree_event_rate_upper"] = get("upper")
    res["tree_event_n_known"] = get("n_known")
    res["tree_event_rate_source"] = source
    return res, dict(state=state, source=source, threshold=thr, leaves=table)


def _auc(p, y):
    pos, neg = p[y == 1], p[y == 0]
    if not len(pos) or not len(neg):
        return None
    r = pd.Series(np.concatenate([pos, neg])).rank().values
    return float((r[:len(pos)].sum() - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def calibration(res, y, tree, rows=None):
    """Predicted vs observed event rate per leaf and overall (AUC, Gini, KS, Brier) on ``rows``."""
    if y is None or "tree_proba_1" not in res:
        return None
    p = res["tree_proba_1"].values.astype(float)
    ok_ = ~np.isnan(y) & ~np.isnan(p)
    if rows is not None:
        ok_ &= rows
    names = {n["id"]: n.get("name") or n["id"] for n in leaves(tree)}
    per = []
    for lid in [lf["id"] for lf in leaves(tree)]:
        m = ok_ & (res["tree_leaf"].values == lid)
        nk = int(m.sum())
        obs = float(y[m].mean()) if nk else None
        pr = float(p[m].mean()) if nk else None
        lo, hi = _wilson(float(y[m].sum()), nk)
        per.append(dict(leaf=lid, name=names.get(lid), n_known=nk, predicted=pr, observed=obs,
                        diff=None if obs is None or pr is None else obs - pr, obs_lower=lo, obs_upper=hi,
                        inside=None if lo is None or pr is None else bool(lo <= pr <= hi)))
    yy, pp = y[ok_], p[ok_]
    auc = _auc(pp, yy)
    ks = None
    if len(yy) and yy.sum() and (1 - yy).sum():
        o = np.argsort(-pp)
        ks = float(np.max(np.abs(np.cumsum(yy[o]) / yy.sum() - np.cumsum(1 - yy[o]) / (1 - yy).sum())))
    return dict(n_known=int(ok_.sum()), observed=float(yy.mean()) if len(yy) else None,
                predicted=float(pp.mean()) if len(pp) else None, auc=auc, gini=None if auc is None else 2 * auc - 1,
                ks=ks, brier=float(np.mean((pp - yy) ** 2)) if len(yy) else None, leaves=per)


# ════════════════════════════════════════════════════════════════════
# relevance (info-score) with pre-binned attributes – fast for 1,500+ columns
# ════════════════════════════════════════════════════════════════════

class Prebin(object):
    """Every candidate attribute coded once into <= 30 fine classes (uint8): the per-node
    relevance of all attributes is then a few vectorised bincounts."""

    def __init__(self, df, columns, target=None, max_bins=20, max_cats=29):
        self.columns = list(columns)
        self.kind = {}
        self.flags = {}
        n = len(df)
        self.codes = np.empty((n, len(self.columns)), dtype=np.uint8)
        tmiss = df[target].isna().values if target and target in df.columns else None
        for j, c in enumerate(self.columns):
            self.codes[:, j], self.kind[c] = prebin_series(df[c], max_bins, max_cats)
            self.flags[c] = attribute_flags(df[c], c, tmiss)

    def relevance(self, mask, y, extra=None, max_rows=200000, seed=7):
        """Per attribute: IV, information gain (share of node entropy), relevance % (best = 100)."""
        known = mask & ~np.isnan(y)
        rows = np.where(known)[0]
        if len(rows) > max_rows:
            rows = np.sort(np.random.RandomState(seed).choice(rows, max_rows, replace=False))
        yk = y[rows]
        out = []
        if len(rows) == 0 or yk.sum() == 0 or yk.sum() == len(yk):
            return out
        sub = self.codes[rows]
        cols = list(self.columns)
        if extra:
            ex = np.column_stack([prebin_series(s, 20, 29)[0] for s in extra.values()]).astype(np.uint8)
            sub = np.hstack([sub, ex[rows]])
            cols += list(extra.keys())
        p = len(cols)
        step = max(1, int(4_000_000 // max(1, len(rows))))
        h0 = _entropy(yk.mean())
        for a in range(0, p, step):
            C = sub[:, a:a + step].astype(np.int32)
            c = C.shape[1]
            flat = (C + np.arange(c, dtype=np.int32) * K).ravel()
            cnt = np.bincount(flat, minlength=c * K).reshape(c, K).astype(float)
            bad = np.bincount(flat, weights=np.repeat(yk, c), minlength=c * K).reshape(c, K)
            good = cnt - bad
            B, G, N = bad.sum(1, keepdims=True), good.sum(1, keepdims=True), cnt.sum(1, keepdims=True)
            use = cnt > 0
            nu = use.sum(1, keepdims=True)
            pb = np.where(use, (bad + 0.5) / (B + 0.5 * nu), 1)
            pg = np.where(use, (good + 0.5) / (G + 0.5 * nu), 1)
            iv = np.sum(np.where(use, (pg - pb) * np.log(pg / pb), 0), axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                pr = np.where(use, bad / np.maximum(cnt, 1), 0)
                hb = np.where((pr > 0) & (pr < 1), -(pr * np.log2(pr) + (1 - pr) * np.log2(1 - pr)), 0)
            hcond = np.sum(cnt / N * hb, axis=1)
            gain = (h0 - hcond) / h0 if h0 > 0 else np.zeros(c)
            for k in range(c):
                name = cols[a + k]
                out.append(dict(variable=name, iv=float(iv[k]), gain=float(max(gain[k], 0)),
                                bins=int(use[k].sum()), kind=self.kind.get(name, "calc"),
                                flags=list(self.flags.get(name, ["calculation"]))))
        best = max((r["gain"] for r in out), default=0) or 1
        for r in out:
            r["relevance"] = 100 * r["gain"] / best
            if r["iv"] >= 2.0 and "suspicious" not in r["flags"]:
                r["flags"] = r["flags"] + ["suspicious"]
        out.sort(key=lambda r: -r["gain"])
        return out


def prebin_series(s, max_bins=20, max_cats=29):
    miss = s.isna().values
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        x = s.values.astype(float)
        xv = x[~np.isnan(x)]
        codes = np.full(len(x), MISS_CODE, dtype=np.uint8)
        if len(xv):
            u = np.unique(xv)
            if len(u) <= max_bins:
                edges = (u[1:] + u[:-1]) / 2
            else:
                edges = np.unique(np.quantile(xv, np.linspace(0, 1, max_bins + 1)[1:-1]))
            codes[~miss] = np.searchsorted(edges, xv, side="right").astype(np.uint8)
        return codes, "numeric"
    v = s.astype(object).where(~miss, None).map(lambda z: None if z is None else str(z))
    top = v.value_counts().index[:max_cats]
    mp = {k: i for i, k in enumerate(top)}
    codes = v.map(lambda z: MISS_CODE if z is None else mp.get(z, OTHER_CODE)).values.astype(np.uint8)
    return codes, "categorical"


def attribute_flags(col, name, target_missing=None):
    flags = []
    miss = col.isna()
    nun = col.nunique(dropna=True)
    if nun <= 1:
        flags.append("constant")
    if miss.mean() > 0.5:
        flags.append("high_missing")
    if target_missing is not None and miss.any() and (miss.values == target_missing).all():
        flags.append("outcome_window")
    numeric = pd.api.types.is_numeric_dtype(col)
    if (not numeric and nun > 0.9 * max(1, col.notna().sum())) or re.search(r"(^|_)id$", name, re.I):
        flags.append("id_like")
    return flags


def guidance(tree, masks, prebin, y, env=None, calcs=None, min_known=100, top=5):
    """Leaves ranked by the information the best next split would add (n_known x gain)."""
    out = []
    for lf in leaves(tree):
        m = masks.get(lf["id"])
        if m is None:
            continue
        kn = m & ~np.isnan(y)
        if kn.sum() < min_known:
            continue
        rel = [r for r in prebin.relevance(m, y) if not set(r["flags"]) & {"outcome_window", "id_like", "constant"}]
        if not rel:
            continue
        b = rel[0]
        out.append(dict(node_id=lf["id"], name=lf.get("name") or lf["id"], n_known=int(kn.sum()),
                        bad_rate=float(np.nanmean(y[kn])), variable=b["variable"], iv=b["iv"], gain=b["gain"],
                        value=float(kn.sum() * b["gain"]), runners_up=[r["variable"] for r in rel[1:4]]))
    out.sort(key=lambda r: -r["value"])
    return out[:top]


# ════════════════════════════════════════════════════════════════════
# optimal-binning split suggestions
# ════════════════════════════════════════════════════════════════════

def suggest_split(x, y, kind=None, max_bins=4, min_share=0.05, monotonic="auto", missing="auto"):
    """Optimal multi-way split of one variable on the rows of a node (optbinning, CP solver).

    x: Series (node rows), y: float array aligned (NaN = unknown outcome).
    Returns a split dict (no children) plus "method".
    """
    numeric = kind == "numeric" if kind else (pd.api.types.is_numeric_dtype(x) and not pd.api.types.is_bool_dtype(x))
    known = ~np.isnan(y)
    xk, yk = x[known], y[known].astype(int)
    if yk.sum() == 0 or yk.sum() == len(yk):
        raise TreeError("The node has only goods or only bads with a known outcome – nothing to split on.")
    method = "optbinning"
    try:
        from optbinning import OptimalBinning
        kw = dict(name=str(x.name), dtype="numerical" if numeric else "categorical", solver="cp",
                  max_n_bins=int(max_bins), min_bin_size=float(min_share))
        if numeric and monotonic and monotonic != "none":
            kw["monotonic_trend"] = {"auto": "auto", "ascending": "ascending", "descending": "descending",
                                     "peak": "peak", "valley": "valley"}.get(monotonic, "auto")
        ob = OptimalBinning(**kw)
        xs = (pd.to_numeric(xk, errors="coerce").values if numeric
              else np.array([str(z) if pd.notna(z) else np.nan for z in xk], dtype=object))
        ob.fit(xs, yk)
        splits = ob.splits
    except ImportError:
        method = "optimal binning (dynamic programming; optbinning not installed)"
        splits = _dp_splits(xk, yk, numeric, max_bins, min_share, monotonic)
    if numeric:
        cuts = sorted(float(c) for c in np.atleast_1d(splits))
        if not cuts:
            raise TreeError(f"No significant split found for {x.name} in this node.")
        split = {"kind": "numeric", "variable": str(x.name), "cuts": cuts}
    else:
        groups = [[str(v) for v in g] for g in splits if len(g)]
        if len(groups) < 2:
            raise TreeError(f"No significant grouping found for {x.name} in this node.")
        split = {"kind": "categorical", "variable": str(x.name), "groups": groups}
        _assign_other(split, x, y)
    _assign_missing(split, x, y, missing)
    split["source"] = "optbinning" if method == "optbinning" else "dp"
    split["method"] = method
    return split


def _dp_splits(xk, yk, numeric, max_bins, min_share, monotonic="auto", max_prebins=50):
    """Optimal binning without optbinning (pure numpy, e.g. in the browser).

    The variable is pre-binned (<= 50 quantile bins, or categories ordered by event rate),
    then dynamic programming finds the contiguous grouping into <= max_bins groups with the
    highest IV, every group holding >= min_share of the rows, and – for numeric variables –
    a monotonic event rate (ascending, descending, or the better of both for "auto").
    Exact for the given pre-bins. Returns cuts (numeric) or category groups.
    """
    if numeric:
        xv = pd.to_numeric(xk, errors="coerce").values.astype(float)
        ok = ~np.isnan(xv)
        xv, yv = xv[ok], yk[ok]
        if not len(xv):
            return []
        u = np.unique(xv)
        edges = (u[1:] + u[:-1]) / 2 if len(u) <= max_prebins else \
            np.unique(np.quantile(xv, np.linspace(0, 1, max_prebins + 1)[1:-1]))
        code = np.searchsorted(edges, xv, side="right")
        P = len(edges) + 1
    else:
        sv = pd.Series(np.asarray(xk, dtype=object)).where(pd.notna(pd.Series(np.asarray(xk, dtype=object))), None)
        ok = sv.notna().values
        sv, yv = sv[ok].astype(str), yk[ok]
        top = sv.value_counts().index[:max_prebins]
        sv = sv[sv.isin(top)]
        yv = yv[sv.index.values] if len(sv) else yv[:0]
        rates = pd.Series(yv, index=sv.values).groupby(level=0).mean().sort_values()
        cats = list(rates.index)
        code = pd.Series(range(len(cats)), index=cats)[sv.values].values
        P = len(cats)
    if P < 2:
        return []
    n = np.bincount(code, minlength=P).astype(float)
    e = np.bincount(code, weights=yv.astype(float), minlength=P)
    cn, ce = np.r_[0, np.cumsum(n)], np.r_[0, np.cumsum(e)]
    N, E = cn[-1], ce[-1]
    G = N - E
    if E == 0 or G == 0:
        return []
    min_n = max(1.0, float(min_share) * N)

    def seg(i, j):  # pre-bins i..j inclusive -> (rows, event rate, IV contribution)
        nn, ee = cn[j + 1] - cn[i], ce[j + 1] - ce[i]
        de, dg = (ee + 0.5) / (E + 0.5), (nn - ee + 0.5) / (G + 0.5)
        return nn, ee / nn if nn else 0.0, (de - dg) * math.log(de / dg)

    S = {(i, j): seg(i, j) for i in range(P) for j in range(i, P)}

    def solve(trend):
        # best[k][(i, j)] = (IV, previous segment) for k groups covering pre-bins 0..j, the last one i..j
        best = [dict() for _ in range(max_bins + 1)]
        for j in range(P):
            if S[(0, j)][0] >= min_n:
                best[1][(0, j)] = (S[(0, j)][2], None)
        for k in range(2, max_bins + 1):
            for (h, i0), (val, _) in best[k - 1].items():
                r_prev = S[(h, i0)][1]
                for j in range(i0 + 1, P):
                    nn, r, iv = S[(i0 + 1, j)]
                    if nn < min_n:
                        continue
                    if (trend == "ascending" and r <= r_prev) or (trend == "descending" and r >= r_prev):
                        continue
                    key = (i0 + 1, j)
                    if key not in best[k] or best[k][key][0] < val + iv:
                        best[k][key] = (val + iv, (h, i0))
        cands = [(best[k][(i, P - 1)][0], k, i) for k in range(2, max_bins + 1)
                 for i in range(P) if (i, P - 1) in best[k]]
        if not cands:
            return None
        val, k, i = max(cands)
        segs, key = [], (i, P - 1)
        while k >= 1 and key is not None:
            segs.append(key)
            key = best[k][key][1]
            k -= 1
        return val, sorted(segs)

    trends = (["none"] if not numeric or monotonic in ("none", "peak", "valley", None, "")
              else ["ascending", "descending"] if monotonic == "auto" else [monotonic])
    sols = [r for r in (solve(t) for t in trends) if r]
    if not sols:
        return []
    segs = max(sols)[1]
    if numeric:
        return [float(edges[j]) for (_, j) in segs[:-1]]
    return [[cats[c] for c in range(i, j + 1)] for (i, j) in segs]


def _tree_splits(xk, yk, numeric, max_bins, min_share):
    from sklearn.tree import DecisionTreeClassifier
    if numeric:
        xv = pd.to_numeric(xk, errors="coerce").values
        ok = ~np.isnan(xv)
        t = DecisionTreeClassifier(max_leaf_nodes=max_bins, min_samples_leaf=max(1, int(min_share * ok.sum())))
        t.fit(xv[ok].reshape(-1, 1), yk[ok])
        return sorted(float(v) for v in t.tree_.threshold[t.tree_.feature >= 0])
    s = xk.astype(str)
    rates = pd.Series(yk, index=s.index).groupby(s).mean()
    r = s.map(rates).values
    t = DecisionTreeClassifier(max_leaf_nodes=max_bins, min_samples_leaf=max(1, int(min_share * len(r))))
    t.fit(r.reshape(-1, 1), yk)
    th = sorted(t.tree_.threshold[t.tree_.feature >= 0])
    grp = np.searchsorted(th, rates.values, side="right")
    return [list(rates.index[grp == g]) for g in range(len(th) + 1)]


def _assign_other(split, x, y):
    listed = {_canon(v) for g in split["groups"] for v in g}
    known = ~np.isnan(y)
    s = x.astype(object).map(_canon)
    rates = [np.nanmean(y[s.isin({_canon(v) for v in g}).values & known]) for g in split["groups"]]
    other = ~s.isin(listed).values & x.notna().values & known
    if other.any():
        ro = np.nanmean(y[other])
        split["other_branch"] = int(np.nanargmin([abs(r - ro) for r in rates]))
    else:
        split["other_branch"] = int(np.nanargmax(rates))  # unseen categories -> riskiest group


def _assign_missing(split, x, y, policy="auto"):
    miss = x.isna().values
    known = ~np.isnan(y)
    nb = n_branches(dict(split, missing_separate=False))
    br = route(dict(split, missing_branch=0, missing_separate=False), Env(pd.DataFrame({split["variable"]: x})), x.index)
    rates = [np.nanmean(y[(br == i) & ~miss & known]) if ((br == i) & ~miss & known).any() else np.nan for i in range(nb)]
    km = miss & known
    share = km.sum() / max(1, known.sum())
    if policy == "auto":
        policy = "separate" if share >= 0.02 else "similar"
    split.pop("missing_separate", None)
    if policy == "separate" and km.any():
        split["missing_separate"] = True
        split["missing_branch"] = nb
    elif policy == "worst" or not km.any():
        split["missing_branch"] = int(np.nanargmax(rates))
    else:
        rm = np.nanmean(y[km])
        split["missing_branch"] = int(np.nanargmin([abs(r - rm) if not np.isnan(r) else np.inf for r in rates]))
    split["missing_policy"] = policy


def split_from_binning_config(var_cfg, variable, x=None, y=None, config_name=""):
    """Turn a Binning v2 saved definition (cut-offs / category groups / missing treatment) into a split."""
    if var_cfg.get("dtype") == "numerical":
        cuts = sorted(float(c) for c in var_cfg.get("cutoffs") or [])
        if not cuts:
            raise TreeError(f"Saved binning of {variable} has no cut-offs.")
        split = {"kind": "numeric", "variable": variable, "cuts": cuts}
    else:
        groups = [[str(v) for v in g] for g in var_cfg.get("cat_groups") or [] if g]
        if len(groups) < 2:
            raise TreeError(f"Saved binning of {variable} has fewer than 2 groups.")
        split = {"kind": "categorical", "variable": variable, "groups": groups, "other_branch": 0}
        if x is not None and y is not None:
            _assign_other(split, x, y)
    t = var_cfg.get("missing", "separate")
    if str(t).startswith("group:"):
        g = int(str(t).split(":", 1)[1]) - 1  # Binning v2 groups are numbered from 1 (0 = missing)
        split["missing_branch"] = min(max(g, 0), n_branches(split) - 1)
        split["missing_policy"] = t
    elif x is not None and y is not None:
        _assign_missing(split, x, y, {"separate": "separate", "worst": "worst"}.get(t, "similar"))
    else:
        split["missing_branch"] = 0
    split["source"] = f"binning_config:{config_name}" if config_name else "binning_config"
    return split


def branch_stats(split, env, index, mask, y):
    """Preview of a split on a node: per-branch counts, bad rates, IV and information gain."""
    br = route(split, env, index)
    labels = branch_labels(split)
    rows, cnt, bad = [], [], []
    nm = int(mask.sum())
    for i, lab in enumerate(labels):
        m = mask & (br == i)
        r = dict(branch=i, label=lab, n=int(m.sum()), share=float(m.sum() / nm) if nm else None)
        if y is not None:
            kn = m & ~np.isnan(y)
            r.update(n_known=int(kn.sum()), bad_rate=float(np.nanmean(y[kn])) if kn.any() else None)
            cnt.append(kn.sum())
            bad.append(np.nansum(y[kn]))
        rows.append(r)
    iv, gain = _iv_gain(cnt, bad) if y is not None else (None, None)
    return dict(branches=rows, iv=iv, gain=gain, bins=bin_table(split, env, index, mask, y))


def _events(r, m, y):
    if y is None:
        r.update(events=None, non_events=None, bad_rate=None)
        return
    kn = m & ~np.isnan(y)
    ev = float(np.nansum(y[kn]))
    r.update(events=int(ev), non_events=int(kn.sum() - ev), bad_rate=float(ev / kn.sum()) if kn.any() else None)


def bin_table(split, env, index, mask, y):
    """Binning v2-style table for manual overrides: one row per bin (missing rows reported apart),
    with events, non-events, event rate, WOE = ln(%events / %non-events), IV contribution, trend,
    and the bounds (numeric) or categories (categorical) needed to merge, move or split bins."""
    k = split["kind"]
    br = route(split, env, index)
    core = branch_labels(dict(split, missing_separate=False, missing_branch=None))
    miss = np.zeros(len(index), dtype=bool)
    x = col = None
    if k != "rule":
        col = env[split["variable"]]
        miss = col.isna().values
        if k == "numeric":
            x = pd.to_numeric(col, errors="coerce").values.astype(float)
            miss = miss | np.isnan(x)
    cuts = [float(c) for c in split.get("cuts") or []]
    groups = split.get("groups") or []
    rows = []
    for i, lab in enumerate(core):
        m = mask & (br == i) & ~miss
        r = dict(group=i, label=lab, n=int(m.sum()))
        _events(r, m, y)
        if k == "numeric":
            v = x[m]
            r.update(lo=cuts[i - 1] if i > 0 else None, hi=cuts[i] if i < len(cuts) else None,
                     min=float(v.min()) if len(v) else None, max=float(v.max()) if len(v) else None,
                     median=float(np.median(v)) if len(v) else None)
        elif k == "categorical":
            vc = col[m].astype(object).map(_canon).value_counts()
            r["members"] = [str(g) for g in groups[i]] if i < len(groups) else None
            r["n_categories"] = int(len(vc))
            r["categories"] = [{"value": str(a), "n": int(b)} for a, b in vc.head(80).items()]
        rows.append(r)
    missing = None
    if k != "rule":
        m = mask & miss
        missing = dict(n=int(m.sum()), to="separate" if split.get("missing_separate") else int(split.get("missing_branch") or 0))
        _events(missing, m, y)
    scored = rows + ([missing] if missing and missing["n"] else [])
    tot_e = sum(r["events"] or 0 for r in scored)
    tot_n = sum(r["non_events"] or 0 for r in scored)
    for r in scored:
        if y is None or not tot_e or not tot_n or not r["n"] or r.get("bad_rate") is None:
            r.update(woe=None, iv_contrib=None)
            continue
        de, dn = max(r["events"], 0.5) / tot_e, max(r["non_events"], 0.5) / tot_n
        r["woe"] = float(np.log(de / dn))
        r["iv_contrib"] = float((de - dn) * r["woe"])
    for i, r in enumerate(rows):
        prev = rows[i - 1].get("bad_rate") if i else None
        cur = r.get("bad_rate")
        r["trend"] = None if prev is None or cur is None or cur == prev else ("up" if cur > prev else "down")
    if missing is not None and "woe" not in missing:
        missing.update(woe=None, iv_contrib=None)
    return dict(kind=k, rows=rows, missing=missing,
                iv=float(sum(r["iv_contrib"] or 0 for r in scored)) if y is not None else None)


def apply_split(tree, node_id, split, leaf_template=None):
    """Turn a leaf (or re-split a node) into a split with one leaf per branch."""
    node, _ = find(tree, node_id)
    split = {k: v for k, v in split.items() if k != "method"}
    old_leaf = node.get("leaf") or (leaf_template or {})
    labels = branch_labels(split)
    kids = []
    for i, lab in enumerate(labels):
        nid = next_id(tree) if not kids else f"n{int(kids[-1]['id'][1:]) + 1}"
        lf = new_leaf(nid, lab if split["kind"] == "rule" else f"{split.get('variable')} {lab}")
        lf["leaf"] = dict(copy.deepcopy(old_leaf), outputs=copy.deepcopy(old_leaf.get("outputs") or []))
        lf["leaf"].setdefault("mode", "cutoff")
        kids.append(lf)
    node["split"] = split
    node["children"] = kids
    node["leaf"] = None
    return tree


def prune(tree, node_id, leaf_template=None):
    node, _ = find(tree, node_id)
    first_leaf = next((l["leaf"] for l in leaves(node) if l.get("leaf")), None)
    node["split"], node["children"] = None, []
    node["leaf"] = copy.deepcopy(first_leaf or leaf_template or new_leaf("x")["leaf"])
    return tree


# ════════════════════════════════════════════════════════════════════
# Visual ML (Dataiku) decision-tree import
# ════════════════════════════════════════════════════════════════════

def from_dss_trees(raw, df, approve_class=None, threshold=0.5, tree_index=0, model_label=""):
    """Rebuild a strategy tree from Dataiku's tree structure (DSSTreeSet.get_raw()).

    raw: {"featureNames": [..], "classes": [..], "trees": [{"leftChild", "rightChild", "feature",
          "threshold", "nSamples", "probas", "impurity", "leftCategories"?}]}
    Numeric thresholds are calibrated against the data: Dataiku may store them in the rescaled
    feature space, so for every split the identity / avg-std / min-max un-rescaling whose left
    share best matches the model's own nSamples is used (reported in the import notes).
    """
    names = raw["featureNames"]
    t = raw["trees"][tree_index]
    classes = [str(c) for c in (raw.get("classes") or [])] or ["0", "1"]
    if approve_class is None:
        approve_class = next((c for c in classes if c.lower() in ("approve", "approved", "0", "good")), classes[0])
    ai = classes.index(str(approve_class)) if str(approve_class) in classes else 0
    L, R = t["leftChild"], t["rightChild"]
    feat, thr = t.get("feature"), t.get("threshold")
    ns, probas, lcat = t.get("nSamples"), t.get("probas"), t.get("leftCategories")
    notes, counter = [], [0]
    env = Env(df)
    stats = {}

    def nid():
        counter[0] += 1
        return f"n{counter[0]}"

    def leaf(i, mask):
        p = probas[i] if probas is not None else None
        pa = float(p[ai]) if p is not None and len(p) > ai else None
        mode = "approve" if pa is not None and pa >= threshold else "decline"
        lf = new_leaf(nid(), f"P({approve_class})={pa:.2f}" if pa is not None else "leaf", mode=mode,
                      reason_codes=[] if mode == "approve" else ["MODEL_DECLINE"])
        lf["leaf"]["outputs"] = [{"name": "model_p_" + re.sub(r"\W+", "_", str(approve_class)).lower(),
                                  "expr": repr(round(pa, 6)) if pa is not None else "None"}]
        lf["imported"] = dict(n_samples=ns[i] if ns else None, probas=p)
        return lf

    def build(i, mask):
        if L[i] < 0:
            return leaf(i, mask)
        fname = names[feat[i]]
        node = {"id": nid(), "name": "", "calcs": [], "leaf": None}
        exp_left = (ns[L[i]] / ns[i]) if ns and ns[i] else None
        order = [L[i], R[i]]  # child model node per branch
        if lcat is not None and lcat[i]:
            col = _source_column(fname, df)
            cats = [str(c) for c in lcat[i]]
            node["split"] = {"kind": "categorical", "variable": col, "groups": [cats], "other_separate": True,
                             "missing_branch": 1, "source": "visual_ml"}
            node["name"] = f"{col} in {cats[:3]}"
        elif fname.startswith("dummy:"):
            _, col, val = fname.split(":", 2)
            if col not in df.columns:
                raise TreeError(f"Model feature '{fname}': column '{col}' not in the dataset.")
            if val in ("N/A", "__DKU_N/A__"):
                # dummy = 1 when missing; left (<= 0.5) = not missing
                node["split"] = {"kind": "rule", "rules": [f"notnull(col({col!r}))"], "source": "visual_ml"}
                node["name"] = f"{col} not missing"
            else:
                # left (dummy <= 0.5) = any other value or missing; right = the value
                node["split"] = {"kind": "categorical", "variable": col, "groups": [[val]], "other_separate": True,
                                 "missing_branch": 1, "source": "visual_ml"}
                node["name"] = f"{col} = {val}"
                order = [R[i], L[i]]
        else:
            col = _source_column(fname, df)
            x = pd.to_numeric(df[col], errors="coerce")
            cut, how, mb = _calibrate(thr[i], x, mask, exp_left)
            notes.append(f"{col}: threshold {thr[i]:.4g} -> {cut:.6g} ({how})")
            node["split"] = {"kind": "numeric", "variable": col, "cuts": [cut], "missing_branch": mb,
                             "source": "visual_ml"}
            node["name"] = f"{col} < {_fmt(cut)}"
        br = route(node["split"], env, df.index)
        node["children"] = [build(c, mask & (br == b)) for b, c in enumerate(order)]
        return node

    tree = build(0, np.ones(len(df), dtype=bool))
    # fidelity: data share per leaf vs the model's training nSamples share
    res, masks, _ = evaluate(df, tree)
    rows = []
    total_ns = ns[0] if ns else None
    for lf in leaves(tree):
        imp = lf.get("imported") or {}
        rows.append(dict(node_id=lf["id"], data_share=float(masks[lf["id"]].mean()),
                         model_share=(imp.get("n_samples") / total_ns) if total_ns and imp.get("n_samples") else None))
    diffs = [abs(r["data_share"] - r["model_share"]) for r in rows if r["model_share"] is not None]
    report = dict(notes=notes, leaves=len(rows), approve_class=str(approve_class), classes=classes,
                  max_share_gap=max(diffs) if diffs else None, leaf_shares=rows, model=model_label)
    return tree, report


def _source_column(fname, df):
    if fname in df.columns:
        return fname
    for part in reversed(fname.split(":")):
        if part in df.columns:
            return part
    raise TreeError(f"Model feature '{fname}' cannot be mapped to a dataset column (unsupported preprocessing).")


def _calibrate(t, x, mask, exp_left):
    xv = x.values.astype(float)
    sel = (mask if mask is not None else np.ones(len(xv), dtype=bool)) & ~np.isnan(xv)
    finite = xv[~np.isnan(xv)]
    n_all = len(xv)
    mean, std = (float(np.mean(finite)), float(np.std(finite, ddof=1))) if len(finite) > 1 else (0.0, 1.0)
    # std after mean imputation of the missing values (rescaling fitted after imputation)
    std_imp = std * math.sqrt((len(finite) - 1) / max(1, n_all - 1)) if len(finite) > 1 else std
    lo, hi = (float(np.min(finite)), float(np.max(finite))) if len(finite) else (0.0, 1.0)
    cands = [("raw", float(t)), ("avg/std un-rescaled", float(t) * (std or 1) + mean),
             ("avg/std un-rescaled, after imputation", float(t) * (std_imp or 1) + mean),
             ("min/max un-rescaled", float(t) * ((hi - lo) or 1) + lo)]
    best = cands[0]
    if exp_left is not None and sel.any():
        best = min(cands, key=lambda c: abs(np.mean(xv[sel] <= c[1]) - exp_left))
    cut = best[1]
    # snap to the midpoint between the two observed values around the cut (sklearn-style threshold)
    u = np.unique(finite)
    k = np.searchsorted(u, cut, side="right")
    if 0 < k < len(u):
        cut = float((u[k - 1] + u[k]) / 2)
    mb = 0 if mean <= cut else 1  # Dataiku imputes missing numerics with the mean by default
    return cut, best[0], mb


# ════════════════════════════════════════════════════════════════════
# exporters
# ════════════════════════════════════════════════════════════════════

def _q(c):
    return tx._q(c)


def _sql_lit(v):
    f = _num(v)
    return repr(f) if f is not None and not isinstance(v, str) else tx.sql_str(v)


_NUMERIC_COLS = [frozenset()]   # numeric columns known to the caller (typed literals for strict SQL engines)


def _cat_lit(var, x):
    """Category value as a SQL literal: a number for numeric columns (BigQuery does not cast), else text."""
    if var in _NUMERIC_COLS[0]:
        f = _num(x)
        if f is not None:
            return repr(float(f))
    return _sql_lit(x)


def _branch_sql(split, i, subst):
    k = split["kind"]
    if k == "rule":
        rules = [f"COALESCE({tx.to_sql(r, subst)}, FALSE)" for r in split["rules"]]
        parts = [f"NOT {r}" for r in rules[:i]] + ([rules[i]] if i < len(rules) else [])
        return "(" + " AND ".join(parts) + ")"
    v = f"({subst[split['variable']]})" if split["variable"] in subst else _q(split["variable"])
    nb = n_branches(split)
    if split.get("missing_separate") and i == nb - 1:
        return f"({v} IS NULL)"
    if k == "numeric":
        c = split["cuts"]
        conds = []
        if i > 0:
            conds.append(f"{v} >= {float(c[i - 1])!r}")
        if i < len(c):
            conds.append(f"{v} < {float(c[i])!r}")
        cond = " AND ".join(conds) or "1=1"
    else:
        groups = split["groups"]
        if i < len(groups):
            cond = f"{v} IN ({', '.join(_cat_lit(split['variable'], x) for x in groups[i])})"
        else:
            cond = None
        listed = [x for g in groups for x in g]
        not_listed = f"{v} NOT IN ({', '.join(_cat_lit(split['variable'], x) for x in listed)})"
        other = len(groups) if split.get("other_separate") else int(split.get("other_branch") or 0)
        if i == other:
            cond = f"({cond} OR {not_listed})" if cond else not_listed
        cond = cond or "1=0"
    if not split.get("missing_separate") and int(split.get("missing_branch") or 0) == i:
        return f"({v} IS NULL OR ({cond}))"
    return f"({v} IS NOT NULL AND ({cond}))"


def _export_rates(tree, leaf_rates):
    """Leaf rates for exported formulas: given ones, else the frozen ones (else none)."""
    if leaf_rates is not None:
        return leaf_rates
    return tree["pd_model"]["leaves"] if pd_state(tree) == "frozen" else {}


def _rates_note(tree, leaf_rates, rates_source, comment):
    if not uses_leaf_rates(tree):
        return ""
    if leaf_rates is None and pd_state(tree) == "frozen":
        rates_source = "frozen leaf event rates"
    return (f"{comment} Leaf formulas use leaf_event_rate: embedded as constants from "
            f"{rates_source or 'no leaf rates (NULL) – freeze the leaf rates first'}\n")


def to_sql(tree, meta, table="input_table", leaf_rates=None, rates_source=None, dialect=None, numeric_columns=None):
    if dialect:
        with tx.sql_dialect(dialect):
            return to_sql(tree, meta, table, leaf_rates, rates_source, numeric_columns=numeric_columns)
    if numeric_columns is not None:
        prev, _NUMERIC_COLS[0] = _NUMERIC_COLS[0], frozenset(numeric_columns)
        try:
            return to_sql(tree, meta, table, leaf_rates, rates_source)
        finally:
            _NUMERIC_COLS[0] = prev
    rates = _export_rates(tree, leaf_rates)
    rows, out_names = [], []
    resolved, lnames = resolve_labels(tree), label_names(tree)
    for lf in leaves(tree):
        path = next(p for n, _, p in walk(tree) if n is lf)
        subst, conds = {}, []
        for node, i in path:
            for c in node.get("calcs") or []:
                subst[c["name"]] = tx.to_sql(c["expr"], subst)
            conds.append(_branch_sql(node["split"], i, subst))
        for c in lf.get("calcs") or []:
            subst[c["name"]] = tx.to_sql(c["expr"], subst)
        L = lf["leaf"]
        mode = L.get("mode", "cutoff")
        rt = rates.get(lf["id"]) or {}
        lit = lambda v: "NULL" if v is None else repr(float(v))
        for k in lnames:
            v = (resolved[lf["id"]].get(k) or (None,))[0]
            subst["label_" + k] = "NULL" if v is None else _sql_lit(str(v))
        subst.update(leaf_id=_sql_lit(lf["id"]), leaf_event_rate=lit(rt.get("pd")),
                     leaf_event_rate_raw=lit(rt.get("raw")), leaf_n_known=lit(rt.get("n_known")))
        expr = L.get("score_expr") or meta.get("score_col")
        score = tx.to_sql(expr, subst) if expr else "NULL"
        subst["leaf_score"] = score
        if mode == "cutoff":
            ra = _num(L.get("refer_at"))
            dec = (f"CASE WHEN {score} IS NULL THEN 'REFER' WHEN {score} >= {_num(L.get('approve_at'))!r} THEN 'APPROVE'"
                   + (f" WHEN {score} >= {ra!r} THEN 'REFER'" if ra is not None else "") + " ELSE 'DECLINE' END")
        elif mode in ("none", "output"):
            dec = "NULL"
        else:
            dec = f"'{mode.upper()}'"
        if mode != "output":
            subst["leaf_decision"] = dec
        outs = {}
        if mode == "output" and L.get("decision_output") not in [o["name"] for o in leaf_outputs(tree, L)]:
            dec = "'REFER'"
        for o in leaf_outputs(tree, L):
            outs[o["name"]] = tx.to_sql(o["expr"], subst)
            subst[o["name"]] = outs[o["name"]]
            if o["name"] not in out_names:
                out_names.append(o["name"])
            if mode == "output" and o["name"] == L.get("decision_output"):
                dec = f"COALESCE(UPPER(TRIM(CAST(({outs[o['name']]}) AS {tx.dialect()['string']}))), 'REFER')"
                subst["leaf_decision"] = dec
        limit = tx.to_sql(L["limit_expr"], subst) if L.get("limit_expr") else "1.0"
        rows.append((" AND ".join(conds) or "1=1", lf, score, dec, limit, outs))

    def case(alias, pick):
        return "  CASE\n" + "\n".join(f"    WHEN {r[0]} THEN {pick(*r[1:])}" for r in rows) + f"\n  END AS {alias}"

    cols = [case("tree_leaf", lambda lf, s, d, l, o: f"'{lf['id']}'"),
            case("tree_score", lambda lf, s, d, l, o: s),
            case("tree_decision", lambda lf, s, d, l, o: d),
            case("reason_codes", lambda lf, s, d, l, o: _sql_lit(";".join(lf["leaf"].get("reason_codes") or []))),
            case("pricing_tier", lambda lf, s, d, l, o: _sql_lit(lf["leaf"].get("pricing_tier") or "")),
            case("limit_multiplier", lambda lf, s, d, l, o: l)]
    for k in lnames:
        cols.append(case(_q("label_" + k), lambda lf, s, d, l, o, k=k: "NULL" if (resolved[lf["id"]].get(k) or (None,))[0] is None
                         else _sql_lit(str(resolved[lf["id"]][k][0]))))
    for nm in out_names:
        cols.append(case(_q("out_" + nm), lambda lf, s, d, l, o, nm=nm: o.get(nm, "NULL")))
    note = _rates_note(tree, leaf_rates, rates_source, "--")
    if pd_state(tree) == "frozen":
        pm = tree["pd_model"]
        pdv = lambda lf: (pm["leaves"].get(lf["id"]) or {}).get("pd")
        thr = pm.get("threshold") if pm.get("threshold") is not None else 0.5
        cols += [case("tree_proba_1", lambda lf, s, d, l, o: "NULL" if pdv(lf) is None else repr(float(pdv(lf)))),
                 case("tree_prediction", lambda lf, s, d, l, o: "NULL" if pdv(lf) is None else str(int(pdv(lf) >= thr)))]
        note += (f"-- Leaf event rates frozen on {pm.get('dataset')} at {pm.get('frozen_at')} "
                 f"({pm.get('n_known')} outcomes, credibility {pm.get('credibility')}, threshold {thr})\n")
    return (f"-- Strategy tree {meta.get('name')} v{meta.get('version', 1)} – Dataiku Strategy Tree Builder\n" + note +
            "SELECT\n  t.*,\n" + ",\n".join(cols) + f"\nFROM {table} t")


def to_python(tree, meta, leaf_rates=None, rates_source=None):
    rates = _export_rates(tree, leaf_rates)
    resolved, lnames = resolve_labels(tree), label_names(tree)
    lines = [f"# Strategy tree {meta.get('name')} v{meta.get('version', 1)} – Dataiku Strategy Tree Builder",
             tx.PY_HELPERS, "",
             "def _dec(v):\n    return 'REFER' if _m(v) else str(v).strip().upper()", "",
             "def decide(row):", "    calc = {}"]
    note = _rates_note(tree, leaf_rates, rates_source, "#")
    if note:
        lines[0] += "\n" + note.rstrip("\n")
    pm = tree["pd_model"] if pd_state(tree) == "frozen" else None
    thr = (pm.get("threshold") if pm and pm.get("threshold") is not None else 0.5)
    if pm:
        lines[0] += (f"\n# Leaf event rates frozen on {pm.get('dataset')} at {pm.get('frozen_at')} "
                     f"({pm.get('n_known')} outcomes, credibility {pm.get('credibility')}, threshold {thr})")

    def rec(n, ind, subst):
        p = "    " * ind
        subst = dict(subst)
        for c in n.get("calcs") or []:
            lines.append(f"{p}calc[{c['name']!r}] = {tx.to_python(c['expr'], subst=subst)}")
            subst[c["name"]] = f"calc[{c['name']!r}]"
        s = n.get("split")
        if not s:
            L = n["leaf"]
            mode = L.get("mode", "cutoff")
            rt = rates.get(n["id"]) or {}
            lit = lambda v: "None" if v is None else repr(float(v))
            labs = {k: (resolved[n["id"]].get(k) or (None,))[0] for k in lnames}
            for k, v in labs.items():
                subst["label_" + k] = repr(v)
            subst.update(leaf_id=repr(n["id"]), leaf_event_rate=lit(rt.get("pd")),
                         leaf_event_rate_raw=lit(rt.get("raw")), leaf_n_known=lit(rt.get("n_known")), leaf_score="score")
            expr = L.get("score_expr") or meta.get("score_col")
            lines.append(f"{p}score = {tx.to_python(expr, subst=subst) if expr else 'None'}  # {n.get('name') or n['id']}")
            if mode == "cutoff":
                ra = _num(L.get("refer_at"))
                lines.append(f"{p}decision = 'REFER' if _m(score) else 'APPROVE' if score >= {_num(L.get('approve_at'))!r} else "
                             + (f"'REFER' if score >= {ra!r} else 'DECLINE'" if ra is not None else "'DECLINE'"))
            elif mode in ("none", "output"):
                lines.append(f"{p}decision = None")
            else:
                lines.append(f"{p}decision = {mode.upper()!r}")
            if mode != "output":
                subst["leaf_decision"] = "decision"
            lines.append(f"{p}out = {{}}")
            for o in leaf_outputs(tree, L):
                lines.append(f"{p}out[{o['name']!r}] = {tx.to_python(o['expr'], subst=subst)}")
                subst[o["name"]] = f"out[{o['name']!r}]"
                if mode == "output" and o["name"] == L.get("decision_output"):
                    lines.append(f"{p}decision = _dec(out[{o['name']!r}])")
                    subst["leaf_decision"] = "decision"
            if mode == "output" and L.get("decision_output") not in [o["name"] for o in leaf_outputs(tree, L)]:
                lines.append(f"{p}decision = 'REFER'")
            lim = tx.to_python(L["limit_expr"], subst=subst) if L.get("limit_expr") else "1.0"
            lines.append(f"{p}return {{'node': {n['id']!r}, 'decision': decision, 'score': score, "
                         f"'reason_codes': {list(L.get('reason_codes') or [])!r}, 'pricing_tier': {L.get('pricing_tier') or ''!r}, "
                         f"'limit_multiplier': {lim}, " + (f"'labels': {labs!r}, " if labs else "") + "'outputs': out, 'calc': calc"
                         + ((lambda v: f", 'proba_1': {v!r}, 'prediction': {None if v is None else int(v >= thr)!r}")(
                             (pm["leaves"].get(n["id"]) or {}).get("pd")) if pm else "")
                         + "}")
            return
        kids = n["children"]
        if s["kind"] == "rule":
            for i, r in enumerate(s["rules"]):
                lines.append(f"{p}{'if' if i == 0 else 'elif'} {tx.to_python(r, subst=subst)}:")
                rec(kids[i], ind + 1, subst)
            lines.append(f"{p}else:")
            rec(kids[-1], ind + 1, subst)
            return
        v = subst.get(s["variable"]) or f"row.get({s['variable']!r})"
        nb = n_branches(s)
        miss_to = nb - 1 if s.get("missing_separate") else int(s.get("missing_branch") or 0)
        lines.append(f"{p}x = {v}")
        if s["kind"] == "numeric":
            lines.append(f"{p}b = {miss_to} if _m(x) else sum(1 for c in {[float(c) for c in s['cuts']]!r} if _n(x) >= c)")
        else:
            lookup = {_canon(val): i for i, g in enumerate(s["groups"]) for val in g}
            other = len(s["groups"]) if s.get("other_separate") else int(s.get("other_branch") or 0)
            lines.append(f"{p}b = {miss_to} if _m(x) else {lookup!r}.get(_canon(x), {other})")
        for i, k in enumerate(kids):
            lines.append(f"{p}{'if' if i == 0 else 'elif'} b == {i}:")
            rec(k, ind + 1, subst)

    rec(tree, 1, {})
    return "\n".join(lines) + "\n"


def backtest_sql(tree, meta, table, target=None, leaf_rates=None, rates_source=None, dialect=None, numeric_columns=None):
    """In-database backtest: the exported SQL of the tree, aggregated per leaf and decision.
    Runs in the warehouse (Snowflake, BigQuery, PostgreSQL...) on the full table; only the summary comes back."""
    if dialect:
        with tx.sql_dialect(dialect):
            return backtest_sql(tree, meta, table, target, leaf_rates, rates_source, numeric_columns=numeric_columns)
    inner = to_sql(tree, meta, table=table, leaf_rates=leaf_rates, rates_source=rates_source, numeric_columns=numeric_columns)
    t = _q(target) if target else None
    known = f"SUM(CASE WHEN {t} IN (0, 1) THEN 1 ELSE 0 END)" if t else "0"
    events = f"SUM(CASE WHEN {t} = 1 THEN 1 ELSE 0 END)" if t else "0"
    sum_p = ",\n  SUM(tree_proba_1) AS sum_p" if pd_state(tree) == "frozen" else ""
    return (f"WITH scored AS (\n{inner}\n)\nSELECT\n  tree_leaf, tree_decision,\n  COUNT(*) AS n,\n  {known} AS n_known,\n"
            f"  {events} AS events{sum_p}\nFROM scored\nGROUP BY tree_leaf, tree_decision")


def backtest_groups(res, df, target=None):
    """Same aggregation as backtest_sql, on an in-memory evaluation."""
    y = target_array(df, target)
    g = pd.DataFrame({"tree_leaf": res["tree_leaf"].values, "tree_decision": res["tree_decision"].values,
                      "known": (~np.isnan(y)).astype(int) if y is not None else 0,
                      "event": np.nan_to_num(y, nan=0.0) if y is not None else 0.0})
    if "tree_proba_1" in res and pd_state_from_res(res):
        g["p"] = res["tree_proba_1"].values
    agg = dict(n=("known", "size"), n_known=("known", "sum"), events=("event", "sum"))
    if "p" in g:
        agg["sum_p"] = ("p", "sum")
    return g.groupby(["tree_leaf", "tree_decision"], dropna=False).agg(**agg).reset_index()


def pd_state_from_res(res):
    src = res.get("tree_event_rate_source")
    return src is not None and len(src) and str(src.iloc[0]).startswith("frozen")


def backtest_summary(groups, margin=None, loss=None):
    """KPIs from per-leaf/decision counts (in-memory or in-database)."""
    g = groups.copy()
    g.columns = [str(c).lower() for c in g.columns]
    for c in ("n", "n_known", "events", "sum_p"):
        if c in g:
            g[c] = pd.to_numeric(g[c], errors="coerce").fillna(0.0)
    n = float(g["n"].sum())
    dec = g["tree_decision"].astype(object).where(g["tree_decision"].notna(), None)
    rate = lambda m: float(g.loc[m, "n"].sum() / n) if n else None
    app = (dec == "APPROVE").values
    ak, ae = float(g.loc[app, "n_known"].sum()), float(g.loc[app, "events"].sum())
    kn, ev = float(g["n_known"].sum()), float(g["events"].sum())
    out = dict(n=int(n), approval_rate=rate(app), refer_rate=rate((dec == "REFER").values),
               decline_rate=rate((dec == "DECLINE").values), no_decision_rate=rate(dec.isna().values),
               n_known=int(kn), event_rate=ev / kn if kn else None, event_rate_approved=ae / ak if ak else None,
               avg_predicted=float(g["sum_p"].sum() / n) if "sum_p" in g and n else None)
    if margin is not None and loss is not None:
        out["profit"] = margin * (ak - ae) - loss * ae
        out["profit_per_row"] = out["profit"] / n if n else None
    return out


def backtest_leaves(mem, db):
    """Per-leaf share and event rate, in memory vs in database."""
    def per(g):
        g = g.copy()
        g.columns = [str(c).lower() for c in g.columns]
        for c in ("n", "n_known", "events"):
            g[c] = pd.to_numeric(g[c], errors="coerce").fillna(0.0)
        t = g.groupby("tree_leaf")[["n", "n_known", "events"]].sum()
        t["share"] = t["n"] / t["n"].sum() if t["n"].sum() else np.nan
        t["event_rate"] = np.where(t["n_known"] > 0, t["events"] / t["n_known"].replace(0, np.nan), np.nan)
        return t
    a, b = per(mem), per(db)
    out = []
    for leaf in sorted(set(a.index) | set(b.index), key=lambda x: (str(x))):
        ra, rb = (a.loc[leaf] if leaf in a.index else None), (b.loc[leaf] if leaf in b.index else None)
        f = lambda r, k: None if r is None or pd.isna(r[k]) else float(r[k])
        out.append(dict(leaf=leaf, n_mem=f(ra, "n"), n_db=f(rb, "n"), share_mem=f(ra, "share"), share_db=f(rb, "share"),
                        event_rate_mem=f(ra, "event_rate"), event_rate_db=f(rb, "event_rate")))
    return out


def to_generic(tree, meta):
    body = {"type": "dataiku_strategy_tree_v2", "version": 2, "meta": meta, "tree": tree}
    body["checksum"] = "sha256:" + hashlib.sha256(json.dumps(tree, sort_keys=True, default=str).encode()).hexdigest()
    return body


class _NotConvertible(Exception):
    pass


_ST_OPS = {"Lt": "<", "LtE": "<=", "Gt": ">", "GtE": ">=", "Eq": "==", "NotEq": "!="}
_ST_FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "==": "==", "!=": "!="}


def _rule_to_studio(expr, yes, no, calc_names, nid, use):
    """Strategy Studio sub-tree sending the rows where ``expr`` is true to ``yes``, the others to ``no``.

    Supports comparisons of a column with a constant, ``and`` / ``or`` / ``not``, ``in [...]``,
    ``between()``, ``isnull()`` / ``notnull()`` and chained comparisons. A comparison with a missing
    value is false (as in the Tree Builder), so every Studio split sends missing values to "no".
    Raises _NotConvertible for arithmetic, calculations, functions or column-to-column comparisons.
    """
    import ast as _a

    def var(n):
        if isinstance(n, _a.Name) and n.id not in ("True", "False", "None"):
            name = n.id
        elif isinstance(n, _a.Call) and getattr(n.func, "id", None) == "col" and len(n.args) == 1 \
                and isinstance(n.args[0], _a.Constant):
            name = n.args[0].value
        else:
            return None
        if name in calc_names:
            raise _NotConvertible(f"it uses the calculation '{name}' – compute it as a dataset column first")
        return name

    def const(n):
        if isinstance(n, _a.Constant) and isinstance(n.value, (int, float, str)) and not isinstance(n.value, bool):
            return True, n.value
        if isinstance(n, _a.UnaryOp) and isinstance(n.op, _a.USub) and isinstance(n.operand, _a.Constant) \
                and isinstance(n.operand.value, (int, float)) and not isinstance(n.operand.value, bool):
            return True, -n.operand.value
        return False, None

    def split(v, op, val, t, f, val2=None):
        return {"id": nid(), "type": "split", "name": "", "variable": v, "op": op, "value": val, "value2": val2,
                "missing": "no", "yes": use(t), "no": use(f)}

    def atom(left, op_node, right, t, f):
        op_name = type(op_node).__name__
        if op_name in ("In", "NotIn"):
            v = var(left)
            if v is None or not isinstance(right, (_a.List, _a.Tuple)):
                raise _NotConvertible("'in' needs a column on the left and a list of values")
            vals = []
            for e in right.elts:
                ok, c = const(e)
                if not ok:
                    raise _NotConvertible("'in' lists must contain constants")
                if "," in str(c):
                    raise _NotConvertible("Strategy Studio lists cannot hold values that contain a comma")
                vals.append(str(c))
            return split(v, "in" if op_name == "In" else "not_in", ", ".join(vals), t, f)
        if op_name not in _ST_OPS:
            raise _NotConvertible(f"operator {op_name} is not supported")
        op = _ST_OPS[op_name]
        v, (ok, c) = var(left), const(right)
        if v is None or not ok:
            v, (ok, c) = var(right), const(left)
            op = _ST_FLIP[op]
        if v is None or not ok:
            raise _NotConvertible("comparisons must be between a column and a constant (no arithmetic)")
        if op in ("<", "<=", ">", ">=") and isinstance(c, str):
            raise _NotConvertible(f"'{op}' needs a number")
        return split(v, op, c, t, f)

    def build(n, t, f):
        if isinstance(n, _a.BoolOp):
            vals = n.values
            if isinstance(n.op, _a.And):        # a and b: a ? (b ? t : f) : f
                node = build(vals[-1], t, f)
                for v in reversed(vals[:-1]):
                    node = build(v, node, f)
                return node
            node = build(vals[-1], t, f)        # a or b: a ? t : (b ? t : f)
            for v in reversed(vals[:-1]):
                node = build(v, t, node)
            return node
        if isinstance(n, _a.UnaryOp) and isinstance(n.op, _a.Not):
            return build(n.operand, f, t)
        if isinstance(n, _a.Constant) and isinstance(n.value, bool):
            return use(t if n.value else f)
        if isinstance(n, _a.Name) and n.id in ("True", "False"):
            return use(t if n.id == "True" else f)
        if isinstance(n, _a.Compare):
            if len(n.ops) == 1:
                return atom(n.left, n.ops[0], n.comparators[0], t, f)
            parts, left = [], n.left            # 300 <= x < 600  ->  (300 <= x) and (x < 600)
            for op_node, right in zip(n.ops, n.comparators):
                parts.append((left, op_node, right))
                left = right
            node = atom(*parts[-1], t, f)
            for pr in reversed(parts[:-1]):
                node = atom(*pr, node, f)
            return node
        if isinstance(n, _a.Call) and getattr(n.func, "id", None) in ("isnull", "notnull", "between"):
            fn, args = n.func.id, n.args
            v = var(args[0]) if args else None
            if v is None:
                raise _NotConvertible(f"{fn}() needs a column")
            if fn == "isnull":
                return {"id": nid(), "type": "split", "name": "", "variable": v, "op": "is_missing", "value": None,
                        "value2": None, "missing": "no", "yes": use(t), "no": use(f)}
            if fn == "notnull":
                return {"id": nid(), "type": "split", "name": "", "variable": v, "op": "is_missing", "value": None,
                        "value2": None, "missing": "no", "yes": use(f), "no": use(t)}
            (ok1, a), (ok2, b) = const(args[1]), const(args[2])
            if not (ok1 and ok2) or isinstance(a, str) or isinstance(b, str):
                raise _NotConvertible("between() needs numeric bounds")
            return split(v, "between", a, t, f, b)
        raise _NotConvertible("only comparisons of columns with constants, and / or / not, in, between, isnull and "
                              "notnull can become Strategy Studio splits")

    return build(tx.parse(expr), yes, no)


def to_studio(tree, meta):
    """Convert to Strategy Studio's binary format (chained splits). Returns (tree, issues).
    Labels are resolved per leaf (inheritance applied) and carried as leaf["labels"]."""
    issues = []
    resolved = resolve_labels(tree)
    calc_names = {c.get("name") for n, _, _ in walk(tree) for c in n.get("calcs") or []}
    ids = [0]
    used = set()

    def nid():
        ids[0] += 1
        return f"s{ids[0]}"

    def renumber(nd):
        nd = copy.deepcopy(nd)

        def go(x):
            x["id"] = nid()
            if x.get("type") == "split":
                go(x["yes"]), go(x["no"])
        go(nd)
        return nd

    def use(nd):
        """A sub-tree can hang under one parent only: the first use takes it, later uses get a copy."""
        if id(nd) in used:
            return renumber(nd)
        used.add(id(nd))
        return nd

    def size(nd):
        return 1 + (size(nd["yes"]) + size(nd["no"]) if nd.get("type") == "split" else 0)

    def leaf(n):
        L = n["leaf"]
        if n.get("calcs"):
            issues.append(f"{n['id']}: node calculations are not supported by Strategy Studio.")
        mode = L.get("mode", "cutoff")
        if mode in ("none", "output"):
            what = "no decision (segment only)" if mode == "none" else f"decision from output '{L.get('decision_output')}'"
            issues.append(f"{n['id']}: {what} becomes REFER (Strategy Studio needs a cut-off or fixed decision).")
            mode = "refer"
        if leaf_outputs(tree, L):
            issues.append(f"{n['id']}: outputs ({', '.join(o['name'] for o in leaf_outputs(tree, L))}) are not carried to Strategy Studio.")
        se = L.get("score_expr") or ""
        if mode == "cutoff" and se and se != meta.get("score_col"):
            issues.append(f"{n['id']}: score formula '{se}' (Strategy Studio uses the score column with % / points adjustments).")
        if L.get("limit_expr") and _num(L["limit_expr"]) is None:
            issues.append(f"{n['id']}: limit formula '{L['limit_expr']}' is not a constant.")
        return {"id": nid(), "type": "leaf", "name": n.get("name") or n["id"],
                "score_adjust": {"type": "none", "value": 0},
                "decision": {"mode": mode, "approve_at": _num(L.get("approve_at")), "refer_at": _num(L.get("refer_at"))},
                "reason_codes": list(L.get("reason_codes") or []), "pricing_tier": L.get("pricing_tier") or "",
                "limit_multiplier": _num(L.get("limit_expr")) if _num(L.get("limit_expr")) is not None else 1.0,
                "labels": {k: v for k, (v, _) in resolved.get(n["id"], {}).items()}}

    def conv(n):
        if not n.get("split"):
            return leaf(n)
        if n.get("calcs"):
            issues.append(f"{n['id']}: node calculations are not supported by Strategy Studio.")
        s, kids = n["split"], [conv(c) for c in n["children"]]
        if s["kind"] == "rule":
            # first true rule wins: rule_0 ? kid_0 : (rule_1 ? kid_1 : ... otherwise)
            snapshot = (ids[0], set(used))
            try:
                node = kids[-1]
                for i in range(len(s["rules"]) - 1, -1, -1):
                    node = _rule_to_studio(s["rules"][i], kids[i], node, calc_names, nid, use)
                    node["name"] = (f"Rule {i + 1}: " + s["rules"][i])[:80]
                if size(node) > 4000:
                    raise _NotConvertible("the converted tree would be too large")
                used.add(id(node))
                return node
            except (_NotConvertible, tx.ExprError) as e:
                ids[0], _ = snapshot[0], used.clear()
                used.update(snapshot[1])
                issues.append(f"{n['id']}: rule split not converted ({e}); only its 'Otherwise' branch is kept.")
                return kids[-1]
        if s.get("variable") in calc_names:
            issues.append(f"{n['id']}: split on the calculation '{s['variable']}' – compute it as a dataset column "
                          f"first (Strategy Studio cannot evaluate calculations).")
        nb = n_branches(s)
        miss_to = nb - 1 if s.get("missing_separate") else int(s.get("missing_branch") or 0)
        if s["kind"] == "numeric":
            conds = [("<", c) for c in s["cuts"]]
        else:
            conds = [("in", ", ".join(str(v) for v in g)) for g in s["groups"]]
            other = len(s["groups"]) if s.get("other_separate") else int(s.get("other_branch") or 0)
            if not s.get("other_separate") and other != len(s["groups"]) - 1:
                issues.append(f"{n['id']}: unlisted categories follow branch {other}; Strategy Studio sends them to the last group.")
        if s.get("missing_separate"):
            conds = [("is_missing", None)] + conds
            kids = [kids[-1]] + kids[:-1]
            miss_to = 0
        # chain: cond_0 ? kid_0 : (cond_1 ? kid_1 : ... kid_last)
        node = kids[-1]
        for j in range(len(conds) - 1, -1, -1):
            op, val = conds[j]
            node = {"id": nid(), "type": "split", "name": (n.get("name") or s["variable"]) if j == 0 else "",
                    "variable": s["variable"], "op": op, "value": val, "value2": None,
                    "missing": "yes" if miss_to == j else "no", "yes": kids[j], "no": node}
        return node

    return conv(tree), issues


# ════════════════════════════════════════════════════════════════════
# misc
# ════════════════════════════════════════════════════════════════════

def now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("_.").upper()


def starter_tree(score_col, cutoff):
    return new_leaf("n1", "All applications", mode="cutoff", score_expr=score_col,
                    approve_at=round(float(cutoff), 0) if cutoff is not None else None,
                    reason_codes=["SCORE_BELOW_CUTOFF"])
