"""
strategy_engine.py
==================
Manual decision-tree strategies on top of a scored population (Strategy Studio).

A strategy is a binary tree (FICO TRIAD-style, built by hand — not trained):

  split  {"id", "type": "split", "name", "variable", "op", "value", "value2",
          "missing": "yes" | "no", "yes": <node>, "no": <node>}
  leaf   {"id", "type": "leaf", "name",
          "score_adjust": {"type": "none" | "pct" | "points" | "set", "value"},
          "decision": {"mode": "cutoff" | "approve" | "refer" | "decline",
                       "approve_at", "refer_at"},
          "reason_codes": [..], "pricing_tier": "", "limit_multiplier": 1.0}

Operators: <  <=  >  >=  ==  !=  in  not_in  between  is_missing.
Rows whose variable is missing follow the node's ``missing`` branch (default "no"),
except for ``is_missing`` itself. Multi-way splits are chained binary splits.

Score adjustment (leaf): pct  -> score * (1 + value/100)   (value -10 = minus 10%)
                         points -> score + value
                         set    -> value
Decision (leaf):  cutoff -> APPROVE if adjusted >= approve_at, REFER if adjusted >= refer_at,
                            else DECLINE;  or a fixed APPROVE / REFER / DECLINE.

Exporters produce an illustrative Equifax InterConnect-style strategy JSON, an illustrative
Experian PowerCurve-style package, a mock FICO decision-tree JSON, the generic tree, SQL CASE expressions and a Python function.
"""

import copy
import datetime
import hashlib
import json
import re

import numpy as np
import pandas as pd

OPS = ["<", "<=", ">", ">=", "==", "!=", "in", "not_in", "between", "is_missing"]
NUMERIC_OPS = {"<", "<=", ">", ">=", "between"}
DECISIONS = ("APPROVE", "REFER", "DECLINE")
BUREAU_PATTERN = re.compile(
    r"vantage|fico|bureau|equifax|efx|inquir|delinq|dpd|bankrupt|collection|utiliz|tradeline|"
    r"credit_age|thin_file|revolv|derog|public_record|chargeoff|trade", re.I)

IC_OPERATORS = {"<": "LESS_THAN", "<=": "LESS_THAN_OR_EQUAL", ">": "GREATER_THAN",
                ">=": "GREATER_THAN_OR_EQUAL", "==": "EQUALS", "!=": "NOT_EQUALS", "in": "IN",
                "not_in": "NOT_IN", "between": "BETWEEN", "is_missing": "IS_MISSING"}


class StrategyError(ValueError):
    pass


# ════════════════════════════════════════════════════════════════════
# Tree helpers
# ════════════════════════════════════════════════════════════════════

def walk(node, depth=0, path=()):
    """Yield (node, depth, path) depth-first; path = tuple of (split_node, branch)."""
    yield node, depth, path
    if node.get("type") == "split":
        yield from walk(node["yes"], depth + 1, path + ((node, "yes"),))
        yield from walk(node["no"], depth + 1, path + ((node, "no"),))


def leaves(tree):
    return [n for n, _, _ in walk(tree) if n.get("type") == "leaf"]


def variables_used(tree):
    out = []
    for n, _, _ in walk(tree):
        if n.get("type") == "split" and n.get("variable") and n["variable"] not in out:
            out.append(n["variable"])
    return out


def new_leaf(node_id, name, approve_at=None, mode="cutoff", reason_codes=None, tier="", limit=1.0,
             adjust=("none", 0)):
    return {"id": node_id, "type": "leaf", "name": name,
            "score_adjust": {"type": adjust[0], "value": adjust[1]},
            "decision": {"mode": mode, "approve_at": approve_at, "refer_at": None},
            "reason_codes": list(reason_codes or []), "pricing_tier": tier, "limit_multiplier": limit}


def _num(v):
    try:
        if v is None or (isinstance(v, str) and not v.strip()):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _list(v):
    if isinstance(v, (list, tuple)):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in str(v or "").split(",") if x.strip()]


def describe_condition(n):
    op, v, v2 = n.get("op"), n.get("value"), n.get("value2")
    var = n.get("variable") or "?"
    if op == "is_missing":
        return f"{var} is missing"
    if op == "between":
        return f"{v} ≤ {var} ≤ {v2}"
    if op in ("in", "not_in"):
        return f"{var} {'in' if op == 'in' else 'not in'} {{{', '.join(_list(v))}}}"
    return f"{var} {op} {v}"


def describe_action(leaf):
    parts = []
    a = leaf.get("score_adjust") or {}
    t, v = a.get("type", "none"), a.get("value")
    if t == "pct" and _num(v):
        parts.append(f"score {'+' if _num(v) > 0 else '−'}{abs(_num(v)):g}%")
    elif t == "points" and _num(v):
        parts.append(f"score {'+' if _num(v) > 0 else '−'}{abs(_num(v)):g} pts")
    elif t == "set" and _num(v) is not None:
        parts.append(f"score = {_num(v):g}")
    d = leaf.get("decision") or {}
    if d.get("mode", "cutoff") == "cutoff":
        txt = f"approve ≥ {_num(d.get('approve_at')):g}" if _num(d.get("approve_at")) is not None else "cut-off ?"
        if _num(d.get("refer_at")) is not None:
            txt += f", refer ≥ {_num(d.get('refer_at')):g}"
        parts.append(txt)
    else:
        parts.append(d["mode"].upper())
    if leaf.get("pricing_tier"):
        parts.append(f"tier {leaf['pricing_tier']}")
    lm = _num(leaf.get("limit_multiplier"))
    if lm is not None and lm != 1:
        parts.append(f"limit ×{lm:g}")
    return " · ".join(parts)


# ════════════════════════════════════════════════════════════════════
# Validation
# ════════════════════════════════════════════════════════════════════

def validate(tree, columns=None, numeric_columns=None):
    """List of {node_id, level: error|warning, message}."""
    issues, ids = [], set()
    cols = set(columns if columns is not None else [])
    nums = set(numeric_columns if numeric_columns is not None else [])

    def add(n, level, msg):
        issues.append({"node_id": n.get("id"), "level": level, "message": msg})

    for n, _, _ in walk(tree):
        if n.get("id") in ids:
            add(n, "error", f"Duplicate node id {n.get('id')}.")
        ids.add(n.get("id"))
        if n.get("type") == "split":
            var, op = n.get("variable"), n.get("op")
            if not var:
                add(n, "error", "Choose a variable.")
            elif cols and var not in cols:
                add(n, "error", f"Variable '{var}' is not in the dataset.")
            if op not in OPS:
                add(n, "error", f"Unknown operator '{op}'.")
            elif op in NUMERIC_OPS:
                if _num(n.get("value")) is None:
                    add(n, "error", "Enter a numeric value.")
                if op == "between" and _num(n.get("value2")) is None:
                    add(n, "error", "Enter the upper value of the range.")
                if var and nums and var in cols and var not in nums:
                    add(n, "warning", f"'{var}' is not numeric; the comparison may route every row to 'no'.")
            elif op in ("in", "not_in") and not _list(n.get("value")):
                add(n, "error", "Enter at least one value.")
            elif op in ("==", "!=") and (n.get("value") is None or str(n.get("value")).strip() == ""):
                add(n, "error", "Enter a value.")
            if "yes" not in n or "no" not in n:
                add(n, "error", "A split needs both a yes and a no branch.")
        elif n.get("type") == "leaf":
            d = n.get("decision") or {}
            mode = d.get("mode", "cutoff")
            if mode not in ("cutoff", "approve", "refer", "decline"):
                add(n, "error", f"Unknown decision '{mode}'.")
            if mode == "cutoff" and _num(d.get("approve_at")) is None:
                add(n, "error", "Set the approve cut-off.")
            ra, aa = _num(d.get("refer_at")), _num(d.get("approve_at"))
            if mode == "cutoff" and ra is not None and aa is not None and ra > aa:
                add(n, "error", "Refer cut-off must be below the approve cut-off.")
            a = n.get("score_adjust") or {}
            if a.get("type", "none") != "none" and _num(a.get("value")) is None:
                add(n, "error", "Enter the score adjustment value.")
            lm = n.get("limit_multiplier")
            if lm not in (None, "") and (_num(lm) is None or _num(lm) < 0):
                add(n, "error", "Credit-limit multiplier must be a non-negative number.")
            if not (n.get("reason_codes") or []) and mode in ("decline", "refer"):
                add(n, "warning", "Declines and referrals usually carry a reason code.")
        else:
            add(n, "error", "Node must be a split or a leaf.")
    return issues


# ════════════════════════════════════════════════════════════════════
# Evaluation
# ════════════════════════════════════════════════════════════════════

def _condition_mask(col, n):
    """Boolean (yes) mask and missing mask for one split over a column."""
    op, v, v2 = n["op"], n.get("value"), n.get("value2")
    missing = col.isna().values
    if op == "is_missing":
        return missing, np.zeros(len(col), dtype=bool)
    if op in NUMERIC_OPS:
        x = pd.to_numeric(col, errors="coerce").values
        a, b = _num(v), _num(v2)
        with np.errstate(invalid="ignore"):
            if op == "<":
                m = x < a
            elif op == "<=":
                m = x <= a
            elif op == ">":
                m = x > a
            elif op == ">=":
                m = x >= a
            else:
                m = (x >= a) & (x <= b)
        return np.nan_to_num(m, nan=0).astype(bool), missing | np.isnan(x)
    if op in ("in", "not_in"):
        vals = set(_list(v))
        s = col.astype(object).where(col.notna(), None).map(lambda z: None if z is None else _canon(z))
        m = s.isin({_canon(x) for x in vals}).values
        return (m if op == "in" else ~m), missing
    # == / != : numeric comparison when both sides are numeric, else text
    a = _num(v)
    xnum = pd.to_numeric(col, errors="coerce")
    if a is not None and xnum.notna().sum() >= col.notna().sum() * 0.99:
        m = (xnum.values == a)
    else:
        m = (col.astype(object).map(lambda z: _canon(z)).values == _canon(v))
    return (m if op == "==" else ~m), missing


def _canon(z):
    """Canonical text for category matching: 1, 1.0 and '1' compare equal."""
    if z is None or (isinstance(z, float) and np.isnan(z)):
        return None
    f = _num(z)
    if f is not None and float(f).is_integer():
        return str(int(f))
    return str(z).strip()


def evaluate(df, tree, score_col):
    """Route every row through the tree. Returns a DataFrame aligned with ``df``."""
    if score_col not in df.columns:
        raise StrategyError(f"Score column '{score_col}' not found.")
    n = len(df)
    score = np.array(pd.to_numeric(df[score_col], errors="coerce"), dtype=float)
    leaf_id = np.empty(n, dtype=object)
    path = np.empty(n, dtype=object)
    adj = np.full(n, np.nan)
    decision = np.empty(n, dtype=object)
    reasons = np.empty(n, dtype=object)
    tier = np.empty(n, dtype=object)
    limit = np.full(n, np.nan)
    node_rows = {}

    def go(node, mask, trail):
        node_rows[node["id"]] = mask
        if node["type"] == "leaf":
            idx = np.where(mask)[0]
            a = node.get("score_adjust") or {}
            t, v = a.get("type", "none"), _num(a.get("value"))
            s = score[idx]
            if t == "pct" and v is not None:
                s = s * (1 + v / 100.0)
            elif t == "points" and v is not None:
                s = s + v
            elif t == "set" and v is not None:
                s = np.full(len(idx), v)
            adj[idx] = s
            d = node.get("decision") or {}
            mode = d.get("mode", "cutoff")
            if mode == "cutoff":
                aa, ra = _num(d.get("approve_at")), _num(d.get("refer_at"))
                dec = np.where(s >= (aa if aa is not None else np.inf), "APPROVE",
                               np.where((ra is not None) & (s >= (ra if ra is not None else np.inf)), "REFER", "DECLINE"))
                dec = np.where(np.isnan(s), "REFER", dec)  # no score -> manual review
            else:
                dec = np.full(len(idx), mode.upper())
            decision[idx] = dec
            leaf_id[idx] = node["id"]
            path[idx] = " > ".join(trail + [node.get("name") or node["id"]])
            reasons[idx] = ";".join(node.get("reason_codes") or [])
            tier[idx] = node.get("pricing_tier") or ""
            lm = _num(node.get("limit_multiplier"))
            limit[idx] = 1.0 if lm is None else lm
            return
        col = df[node["variable"]]
        yes, miss = _condition_mask(col, node)
        if node.get("op") != "is_missing":
            yes = np.where(miss, node.get("missing", "no") == "yes", yes)
        label = describe_condition(node)
        go(node["yes"], mask & yes, trail + [label])
        go(node["no"], mask & ~yes, trail + ["NOT " + label])

    go(tree, np.ones(n, dtype=bool), [])
    out = pd.DataFrame({
        "strategy_leaf": leaf_id, "strategy_path": path, "adjusted_score": adj,
        "strategy_decision": decision, "reason_codes": reasons, "pricing_tier": tier,
        "limit_multiplier": limit,
    }, index=df.index)
    return out, node_rows


# ════════════════════════════════════════════════════════════════════
# Backtest
# ════════════════════════════════════════════════════════════════════

def _target(df, target):
    if not target or target not in df.columns:
        return None
    y = np.array(pd.to_numeric(df[target], errors="coerce"), dtype=float)  # writable copy
    y[~np.isin(y, [0.0, 1.0])] = np.nan
    return y


def _block(mask, y, decision, margin, loss):
    n = int(mask.sum())
    app = mask & (decision == "APPROVE")
    ref = mask & (decision == "REFER")
    dec = mask & (decision == "DECLINE")
    out = dict(n=n, approved=int(app.sum()), referred=int(ref.sum()), declined=int(dec.sum()),
               approval_rate=(app.sum() / n) if n else None, refer_rate=(ref.sum() / n) if n else None,
               decline_rate=(dec.sum() / n) if n else None)
    if y is not None:
        known = mask & ~np.isnan(y)
        kapp = app & ~np.isnan(y)
        bads, bads_app = float(np.nansum(y[known])), float(np.nansum(y[kapp]))
        goods_app = float(kapp.sum() - bads_app)
        out.update(n_known=int(known.sum()), bad_rate=(bads / known.sum()) if known.sum() else None,
                   bad_rate_approved=(bads_app / kapp.sum()) if kapp.sum() else None,
                   bads_approved=int(bads_app), goods_approved=int(goods_app),
                   profit=(margin * goods_app - loss * bads_app) if margin is not None and loss is not None else None)
    return out


def backtest(df, tree, score_col, target=None, margin=None, loss=None, baseline_cutoff=None):
    res, node_rows = evaluate(df, tree, score_col)
    y = _target(df, target)
    decision = res["strategy_decision"].values
    n = len(df)
    nodes = {}
    for node, depth, _ in walk(tree):
        m = node_rows.get(node["id"], np.zeros(n, dtype=bool))
        b = _block(m, y, decision, margin, loss)
        b["share"] = (b["n"] / n) if n else None
        if node["type"] == "leaf":
            sc = res["adjusted_score"].values[m]
            b["avg_adjusted_score"] = float(np.nanmean(sc)) if m.sum() and not np.all(np.isnan(sc)) else None
        nodes[node["id"]] = b
    total = _block(np.ones(n, dtype=bool), y, decision, margin, loss)
    baseline = None
    if baseline_cutoff is not None:
        s = pd.to_numeric(df[score_col], errors="coerce").values
        base_dec = np.where(s >= baseline_cutoff, "APPROVE", "DECLINE")
        baseline = _block(np.ones(n, dtype=bool), y, base_dec, margin, loss)
        baseline["cutoff"] = float(baseline_cutoff)
        moved = decision != base_dec
        baseline["decisions_changed"] = int(moved.sum())
    leaf_rows = []
    for lf in leaves(tree):
        b = dict(nodes[lf["id"]])
        b.update(id=lf["id"], name=lf.get("name"), action=describe_action(lf))
        leaf_rows.append(b)
    return dict(total=total, baseline=baseline, nodes=nodes, leaves=leaf_rows), res


def compare(df, champion, challenger, score_col, target=None, margin=None, loss=None):
    """Champion vs challenger: KPIs of both and the swap set."""
    a, ra = backtest(df, champion, score_col, target, margin, loss)
    b, rb = backtest(df, challenger, score_col, target, margin, loss)
    y = _target(df, target)
    da, db = ra["strategy_decision"].values, rb["strategy_decision"].values
    swaps = []
    for x in DECISIONS:
        for z in DECISIONS:
            m = (da == x) & (db == z)
            row = dict(champion=x, challenger=z, n=int(m.sum()))
            if y is not None:
                k = m & ~np.isnan(y)
                row["bad_rate"] = float(np.nansum(y[k]) / k.sum()) if k.sum() else None
            swaps.append(row)
    return dict(champion=a["total"], challenger=b["total"], swaps=swaps)


# ════════════════════════════════════════════════════════════════════
# Templates
# ════════════════════════════════════════════════════════════════════

def template(name, score_col, cutoff, columns=()):
    cutoff = round(float(cutoff), 0) if cutoff is not None else 600.0
    if name == "cutoff_only":
        return new_leaf("n1", "Score cut-off", approve_at=cutoff, reason_codes=["SCORE_BELOW_CUTOFF"], tier="B")
    if name == "delinquency_override":
        delinq = next((c for c in ("delinquencies_24m_count", "collections_count", "bankruptcy_flag",
                                   "inquiries_6m_count") if c in columns), None)
        if delinq is None:
            raise StrategyError("No bureau delinquency attribute found for this template.")
        return {
            "id": "n1", "type": "split", "name": "Score above cut-off", "variable": score_col,
            "op": ">=", "value": cutoff, "value2": None, "missing": "no",
            "yes": {
                "id": "n2", "type": "split", "name": "Recent delinquency", "variable": delinq,
                "op": ">", "value": 0, "value2": None, "missing": "no",
                "yes": new_leaf("n4", "Delinquent – penalise score", approve_at=cutoff, tier="C",
                                limit=0.8, reason_codes=["DELINQ_24M"], adjust=("pct", -10)),
                "no": new_leaf("n5", "Clean file – approve", approve_at=cutoff, tier="A", limit=1.0),
            },
            "no": new_leaf("n3", "Below cut-off – decline", mode="decline",
                           reason_codes=["SCORE_BELOW_CUTOFF"]),
        }
    raise StrategyError(f"Unknown template '{name}'.")


# ════════════════════════════════════════════════════════════════════
# Exporters
# ════════════════════════════════════════════════════════════════════

def input_source(col, score_col):
    if col == score_col or col.startswith("points_"):
        return "DATAIKU_SCORECARD"
    if BUREAU_PATTERN.search(col):
        return "EQUIFAX_CONSUMER_CREDIT_FILE"
    return "APPLICATION_DATA"


def _ic_actions(leaf):
    acts = []
    a = leaf.get("score_adjust") or {}
    if a.get("type", "none") != "none":
        acts.append({"type": "ADJUST_SCORE", "method": {"pct": "PERCENT", "points": "POINTS", "set": "SET"}[a["type"]],
                     "value": _num(a.get("value"))})
    d = leaf.get("decision") or {}
    if d.get("mode", "cutoff") == "cutoff":
        acts.append({"type": "SET_DECISION", "method": "SCORE_CUTOFF", "scoreField": "adjustedScore",
                     "approveAtOrAbove": _num(d.get("approve_at")), "referAtOrAbove": _num(d.get("refer_at")),
                     "otherwise": "DECLINE"})
    else:
        acts.append({"type": "SET_DECISION", "method": "FIXED", "decision": d["mode"].upper()})
    if leaf.get("reason_codes"):
        acts.append({"type": "SET_REASON_CODES", "codes": list(leaf["reason_codes"])})
    if leaf.get("pricing_tier"):
        acts.append({"type": "SET_PRICING_TIER", "tier": leaf["pricing_tier"]})
    lm = _num(leaf.get("limit_multiplier"))
    if lm is not None:
        acts.append({"type": "SET_CREDIT_LIMIT_MULTIPLIER", "value": lm})
    return acts


def _ic_value(n):
    op = n.get("op")
    if op == "is_missing":
        return None
    if op == "between":
        return [_num(n.get("value")), _num(n.get("value2"))]
    if op in ("in", "not_in"):
        return _list(n.get("value"))
    return _num(n.get("value")) if _num(n.get("value")) is not None else n.get("value")


def to_interconnect(tree, meta, column_types=None, backtest_summary=None):
    """Illustrative Equifax InterConnect-style decision strategy (not an official Equifax spec)."""
    score_col = meta.get("score_col", "score")
    column_types = column_types or {}
    nodes = []
    for n, depth, _ in walk(tree):
        if n["type"] == "split":
            nodes.append({
                "nodeId": n["id"], "label": n.get("name") or describe_condition(n), "type": "CONDITION",
                "condition": {"attribute": n["variable"], "operator": IC_OPERATORS[n["op"]], "value": _ic_value(n)},
                "missingValueRoute": "TRUE" if n.get("missing") == "yes" else "FALSE",
                "onTrue": n["yes"]["id"], "onFalse": n["no"]["id"],
            })
        else:
            nodes.append({"nodeId": n["id"], "label": n.get("name") or n["id"], "type": "OUTCOME",
                          "actions": _ic_actions(n)})
    inputs = [{"name": v, "dataType": "NUMBER" if column_types.get(v) == "numeric" else "STRING",
               "source": input_source(v, score_col)} for v in [score_col] + [x for x in variables_used(tree) if x != score_col]]
    flow = {"startNode": tree["id"], "nodes": nodes}
    checksum = hashlib.sha256(json.dumps(flow, sort_keys=True).encode()).hexdigest()
    return {
        "schemaNote": ("Illustrative Equifax InterConnect-style decision strategy generated by Dataiku Strategy Studio "
                       "for demonstration purposes; field names are not an official Equifax specification."),
        "artifactType": "DECISION_STRATEGY",
        "targetPlatform": "Equifax InterConnect",
        "schemaVersion": "1.0-demo",
        "strategy": {
            "strategyId": meta.get("strategy_id"), "name": meta.get("name"), "version": meta.get("version", 1),
            "status": meta.get("status", "DRAFT"), "description": meta.get("description", ""),
            "effectiveDate": meta.get("effective_date") or datetime.date.today().isoformat(),
            "author": meta.get("author", ""),
            "source": {"platform": "Dataiku", "project": meta.get("project", ""), "dataset": meta.get("dataset", ""),
                       "scorecard": {"scoreField": score_col, "baselineCutoff": meta.get("baseline_cutoff")}},
        },
        "inputs": inputs,
        "outputs": [
            {"name": "decision", "dataType": "ENUM", "values": list(DECISIONS)},
            {"name": "adjustedScore", "dataType": "NUMBER"},
            {"name": "reasonCodes", "dataType": "STRING_LIST"},
            {"name": "pricingTier", "dataType": "STRING"},
            {"name": "creditLimitMultiplier", "dataType": "NUMBER"},
            {"name": "strategyNodeId", "dataType": "STRING"},
        ],
        "decisionFlow": flow,
        "backtest": backtest_summary or {},
        "governance": {"checksum": "sha256:" + checksum, "generatedAt": _now(), "generatedBy": "Dataiku Strategy Studio"},
    }


def to_fico(tree, meta):
    """Mock FICO decision-tree JSON (illustrative)."""
    def node(n):
        if n["type"] == "leaf":
            d = n.get("decision") or {}
            return {"action": {"name": n.get("name"), "scoreAdjustment": n.get("score_adjust"),
                               "decision": d.get("mode", "cutoff").upper() if d.get("mode") != "cutoff" else
                               {"approveIfScoreAtLeast": _num(d.get("approve_at")), "referIfScoreAtLeast": _num(d.get("refer_at"))},
                               "reasonCodes": n.get("reason_codes") or [], "pricingTier": n.get("pricing_tier") or None,
                               "limitMultiplier": _num(n.get("limit_multiplier"))}}
        cond = describe_condition(n)
        return {"partition": {"variable": n["variable"], "name": n.get("name"),
                              "branches": [{"condition": cond, "missingGoesHere": n.get("missing") == "yes", "node": node(n["yes"])},
                                           {"condition": "otherwise", "missingGoesHere": n.get("missing") != "yes", "node": node(n["no"])}]}}
    return {"note": "Mock FICO Platform decision-tree export generated by Dataiku Strategy Studio (illustrative format).",
            "format": "FICO_DECISION_TREE_MOCK", "treeName": meta.get("name"), "version": meta.get("version", 1),
            "variables": [meta.get("score_col", "score")] + variables_used(tree), "root": node(tree)}


# ── Experian PowerCurve (illustrative) ──────────────────────────────────────

PC_OPERATORS = {"<": "LT", "<=": "LE", ">": "GT", ">=": "GE", "==": "EQ", "!=": "NE", "in": "IN",
                "not_in": "NOT_IN", "between": "BETWEEN", "is_missing": "IS_MISSING"}
PC_DATA_AREAS = {"DATAIKU_SCORECARD": "SCORE", "EQUIFAX_CONSUMER_CREDIT_FILE": "BUREAU",
                 "APPLICATION_DATA": "APPLICATION"}


def pc_name(col):
    """PowerCurve-style characteristic name: upper snake case."""
    return re.sub(r"[^A-Za-z0-9]+", "_", str(col)).strip("_").upper()


def _pc_assignments(leaf, score_char):
    """Decision setter of one terminal node, as ordered output assignments."""
    out = []
    a = leaf.get("score_adjust") or {}
    t, v = a.get("type", "none"), _num(a.get("value"))
    expr = (f"{score_char} * {1 + v / 100.0:.12g}" if t == "pct" and v is not None else
            f"{score_char} + ({v:.12g})" if t == "points" and v is not None else
            f"{v:.12g}" if t == "set" and v is not None else score_char)
    out.append({"output": "ADJUSTED_SCORE", "type": "EXPRESSION", "expression": expr})
    d = leaf.get("decision") or {}
    if d.get("mode", "cutoff") == "cutoff":
        rows = [{"from": _num(d.get("approve_at")), "to": None, "value": "APPROVE"}]
        if _num(d.get("refer_at")) is not None:
            rows.append({"from": _num(d.get("refer_at")), "to": _num(d.get("approve_at")), "value": "REFER"})
        out.append({"output": "DECISION", "type": "RANGE_TABLE", "on": "ADJUSTED_SCORE",
                    "ranges": rows, "default": "DECLINE", "missing": "REFER"})
    else:
        out.append({"output": "DECISION", "type": "CONSTANT", "value": d["mode"].upper()})
    out.append({"output": "REASON_CODES", "type": "CONSTANT", "value": list(leaf.get("reason_codes") or [])})
    out.append({"output": "PRICING_TIER", "type": "CONSTANT", "value": leaf.get("pricing_tier") or ""})
    lm = _num(leaf.get("limit_multiplier"))
    out.append({"output": "CREDIT_LIMIT_MULTIPLIER", "type": "CONSTANT", "value": 1.0 if lm is None else lm})
    out.append({"output": "STRATEGY_NODE_ID", "type": "CONSTANT", "value": leaf["id"]})
    return out


def to_powercurve(tree, meta, column_types=None, backtest_summary=None):
    """Illustrative Experian PowerCurve Strategy Management-style package (not an official Experian spec).

    Data dictionary (characteristics by data area) + one decision tree whose split nodes
    branch on a characteristic and whose terminal nodes point to decision setters.
    """
    score_col = meta.get("score_col", "score")
    column_types = column_types or {}
    used = [score_col] + [x for x in variables_used(tree) if x != score_col]
    chars = [{"name": pc_name(v), "sourceField": v,
              "dataType": "NUMERIC" if column_types.get(v) == "numeric" else "STRING",
              "dataArea": PC_DATA_AREAS[input_source(v, score_col)]} for v in used]
    score_char = pc_name(score_col)
    nodes, setters = [], []
    for n, depth, _ in walk(tree):
        if n["type"] == "split":
            cond = {"characteristic": pc_name(n["variable"]), "operator": PC_OPERATORS[n["op"]],
                    "value": _ic_value(n)}
            miss_yes = n.get("missing") == "yes" or n["op"] == "is_missing"
            nodes.append({
                "nodeId": n["id"], "nodeType": "SPLIT", "label": n.get("name") or describe_condition(n),
                "characteristic": pc_name(n["variable"]), "level": depth,
                "branches": [
                    {"label": describe_condition(n), "condition": cond, "includesMissing": miss_yes,
                     "childNodeId": n["yes"]["id"]},
                    {"label": "Otherwise", "condition": "OTHERWISE", "includesMissing": not miss_yes,
                     "childNodeId": n["no"]["id"]},
                ]})
        else:
            sid = "DS_" + pc_name(n["id"])
            nodes.append({"nodeId": n["id"], "nodeType": "TERMINAL", "label": n.get("name") or n["id"],
                          "level": depth, "decisionSetter": sid})
            setters.append({"decisionSetterId": sid, "name": n.get("name") or n["id"],
                            "summary": describe_action(n), "assignments": _pc_assignments(n, score_char)})
    tree_obj = {"treeId": "DT_" + (safe_name(meta.get("name")) or "STRATEGY"), "treeType": "DECISION_TREE",
                "rootNodeId": tree["id"], "nodes": nodes}
    checksum = hashlib.sha256(json.dumps([tree_obj, setters], sort_keys=True).encode()).hexdigest()
    return {
        "schemaNote": ("Illustrative Experian PowerCurve Strategy Management-style package generated by Dataiku "
                       "Strategy Studio for demonstration purposes; element names are not an official Experian "
                       "specification. Map the characteristics to your PowerCurve data dictionary before import."),
        "artifactType": "POWERCURVE_STRATEGY_PACKAGE",
        "targetPlatform": "Experian PowerCurve Strategy Management",
        "schemaVersion": "1.0-demo",
        "strategy": {
            "strategyId": meta.get("strategy_id"), "name": meta.get("name"), "version": meta.get("version", 1),
            "status": meta.get("status", "DRAFT"), "description": meta.get("description", ""),
            "effectiveDate": meta.get("effective_date") or datetime.date.today().isoformat(),
            "author": meta.get("author", ""),
            "championChallenger": {"role": {"CHAMPION": "CHAMPION", "CHALLENGER": "CHALLENGER"}.get(
                meta.get("status"), "UNASSIGNED"), "note": "Set the random-digit allocation in PowerCurve."},
            "source": {"platform": "Dataiku", "project": meta.get("project", ""), "dataset": meta.get("dataset", ""),
                       "scorecard": {"scoreCharacteristic": score_char, "baselineCutoff": meta.get("baseline_cutoff")}},
        },
        "dataDictionary": {
            "characteristics": chars,
            "outputs": [
                {"name": "ADJUSTED_SCORE", "dataType": "NUMERIC"},
                {"name": "DECISION", "dataType": "STRING", "values": list(DECISIONS)},
                {"name": "REASON_CODES", "dataType": "STRING_ARRAY"},
                {"name": "PRICING_TIER", "dataType": "STRING"},
                {"name": "CREDIT_LIMIT_MULTIPLIER", "dataType": "NUMERIC"},
                {"name": "STRATEGY_NODE_ID", "dataType": "STRING"},
            ],
        },
        "decisionTree": tree_obj,
        "decisionSetters": setters,
        "backtest": backtest_summary or {},
        "governance": {"checksum": "sha256:" + checksum, "generatedAt": _now(), "generatedBy": "Dataiku Strategy Studio"},
    }


def _tree_from_powercurve(obj):
    inv = {v: k for k, v in PC_OPERATORS.items()}
    src = {c["name"]: c.get("sourceField") or c["name"] for c in obj.get("dataDictionary", {}).get("characteristics", [])}
    by_id = {n["nodeId"]: n for n in obj["decisionTree"]["nodes"]}
    setters = {s["decisionSetterId"]: s for s in obj.get("decisionSetters", [])}

    def build(nid):
        n = by_id[nid]
        if n["nodeType"] == "SPLIT":
            yes, no = n["branches"][0], n["branches"][1]
            c = yes["condition"]
            op = inv[c["operator"]]
            val, val2 = c.get("value"), None
            if op == "between":
                val, val2 = val[0], val[1]
            elif op in ("in", "not_in"):
                val = ", ".join(str(x) for x in val)
            val, val2 = [int(x) if isinstance(x, float) and x.is_integer() else x for x in (val, val2)]
            return {"id": nid, "type": "split", "name": n.get("label"), "variable": src.get(c["characteristic"], c["characteristic"]),
                    "op": op, "value": val, "value2": val2,
                    "missing": "yes" if yes.get("includesMissing") and op != "is_missing" else "no",
                    "yes": build(yes["childNodeId"]), "no": build(no["childNodeId"])}
        leaf = new_leaf(nid, n.get("label"))
        score_char = obj["strategy"]["source"]["scorecard"]["scoreCharacteristic"]
        for a in setters.get(n.get("decisionSetter"), {}).get("assignments", []):
            o = a["output"]
            if o == "ADJUSTED_SCORE":
                e = a["expression"].strip()
                m = re.fullmatch(re.escape(score_char) + r" \* ([-0-9.eE+]+)", e)
                p = re.fullmatch(re.escape(score_char) + r" \+ \(([-0-9.eE+]+)\)", e)
                if m:
                    leaf["score_adjust"] = {"type": "pct", "value": round((float(m.group(1)) - 1) * 100, 10)}
                elif p:
                    leaf["score_adjust"] = {"type": "points", "value": float(p.group(1))}
                elif _num(e) is not None:
                    leaf["score_adjust"] = {"type": "set", "value": _num(e)}
            elif o == "DECISION" and a["type"] == "RANGE_TABLE":
                ap = next((r["from"] for r in a["ranges"] if r["value"] == "APPROVE"), None)
                rf = next((r["from"] for r in a["ranges"] if r["value"] == "REFER"), None)
                leaf["decision"] = {"mode": "cutoff", "approve_at": ap, "refer_at": rf}
            elif o == "DECISION":
                leaf["decision"] = {"mode": a["value"].lower(), "approve_at": None, "refer_at": None}
            elif o == "REASON_CODES":
                leaf["reason_codes"] = list(a["value"])
            elif o == "PRICING_TIER":
                leaf["pricing_tier"] = a["value"]
            elif o == "CREDIT_LIMIT_MULTIPLIER":
                leaf["limit_multiplier"] = a["value"]
        return leaf

    return build(obj["decisionTree"]["rootNodeId"])


def to_generic(tree, meta):
    return {"type": "dataiku_strategy_tree", "version": 1, "meta": meta, "tree": tree}


def _q(c):
    return '"' + str(c).replace('"', '""') + '"'


def _lit(v):
    f = _num(v)
    return repr(f) if f is not None else "'" + str(v).replace("'", "''") + "'"


def _sql_cond(n, branch):
    col, op = _q(n["variable"]), n["op"]
    if op == "is_missing":
        c = f"{col} IS NULL"
        return c if branch == "yes" else f"{col} IS NOT NULL"
    if op == "between":
        c = f"{col} BETWEEN {_lit(n['value'])} AND {_lit(n['value2'])}"
    elif op in ("in", "not_in"):
        c = f"{col} {'IN' if op == 'in' else 'NOT IN'} ({', '.join(_lit(x) for x in _list(n['value']))})"
    else:
        c = f"{col} {'<>' if op == '!=' else '=' if op == '==' else op} {_lit(n['value'])}"
    miss_yes = n.get("missing") == "yes"
    if branch == "yes":
        return f"({col} IS NULL OR {c})" if miss_yes else f"({col} IS NOT NULL AND {c})"
    return f"({col} IS NOT NULL AND NOT ({c}))" if miss_yes else f"({col} IS NULL OR NOT ({c}))"


def to_sql(tree, meta, table="input_table"):
    score = _q(meta.get("score_col", "score"))
    rows = []
    for lf in leaves(tree):
        path = next(p for n, _, p in walk(tree) if n is lf)
        cond = " AND ".join(_sql_cond(s, b) for s, b in path) or "1=1"
        a = lf.get("score_adjust") or {}
        t, v = a.get("type", "none"), _num(a.get("value"))
        adj = (f"{score} * {1 + v / 100.0!r}" if t == "pct" and v is not None else
               f"{score} + ({v!r})" if t == "points" and v is not None else
               f"{v!r}" if t == "set" and v is not None else score)
        d = lf.get("decision") or {}
        if d.get("mode", "cutoff") == "cutoff":
            ra = _num(d.get("refer_at"))
            dec = (f"CASE WHEN {adj} >= {_num(d.get('approve_at'))!r} THEN 'APPROVE'"
                   + (f" WHEN {adj} >= {ra!r} THEN 'REFER'" if ra is not None else "") + " ELSE 'DECLINE' END")
        else:
            dec = f"'{d['mode'].upper()}'"
        rows.append((cond, lf, adj, dec))
    leaf_case = "  CASE\n" + "\n".join(f"    WHEN {c} THEN '{lf['id']}'" for c, lf, _, _ in rows) + "\n  END AS strategy_leaf"
    adj_case = "  CASE\n" + "\n".join(f"    WHEN {c} THEN {a}" for c, _, a, _ in rows) + "\n  END AS adjusted_score"
    dec_case = "  CASE\n" + "\n".join(f"    WHEN {c} THEN {d}" for c, _, _, d in rows) + "\n  END AS strategy_decision"
    tier_case = "  CASE\n" + "\n".join(f"    WHEN {c} THEN '{lf.get('pricing_tier') or ''}'" for c, lf, _, _ in rows) + "\n  END AS pricing_tier"
    return (f"-- Strategy {meta.get('name')} v{meta.get('version', 1)} ({meta.get('strategy_id')}) "
            f"generated by Dataiku Strategy Studio\nSELECT\n  t.*,\n"
            + ",\n".join([leaf_case, adj_case, dec_case, tier_case]) + f"\nFROM {table} t")


def to_python(tree, meta):
    score = meta.get("score_col", "score")
    lines = [f"# Strategy {meta.get('name')} v{meta.get('version', 1)} ({meta.get('strategy_id')}) - Dataiku Strategy Studio",
             "import math", "", "def _missing(v):",
             "    return v is None or (isinstance(v, float) and math.isnan(v))", "",
             "def decide(row):", f"    score = row.get({score!r})"]

    def cond_py(n):
        var, op, v = n["variable"], n["op"], n.get("value")
        x = f"row.get({var!r})"
        if op == "is_missing":
            return f"_missing({x})"
        if op == "between":
            c = f"{_num(v)!r} <= float({x}) <= {_num(n.get('value2'))!r}"
        elif op in ("in", "not_in"):
            c = f"str({x}) {'in' if op == 'in' else 'not in'} {set(_list(v))!r}"
        elif _num(v) is not None:
            c = f"float({x}) {op} {_num(v)!r}"
        else:
            c = f"str({x}) {op} {str(v)!r}"
        return f"({n.get('missing') == 'yes'!r} if _missing({x}) else {c})"

    def rec(n, ind):
        p = "    " * ind
        if n["type"] == "split":
            lines.append(f"{p}if {cond_py(n)}:  # {n.get('name') or ''}")
            rec(n["yes"], ind + 1)
            lines.append(f"{p}else:")
            rec(n["no"], ind + 1)
            return
        a = n.get("score_adjust") or {}
        t, v = a.get("type", "none"), _num(a.get("value"))
        adj = (f"score * {1 + v / 100.0!r}" if t == "pct" and v is not None else
               f"score + {v!r}" if t == "points" and v is not None else
               f"{v!r}" if t == "set" and v is not None else "score")
        d = n.get("decision") or {}
        lines.append(f"{p}adjusted = {adj}  # {n.get('name') or n['id']}")
        if d.get("mode", "cutoff") == "cutoff":
            ra = _num(d.get("refer_at"))
            lines.append(f"{p}decision = 'APPROVE' if adjusted >= {_num(d.get('approve_at'))!r} else "
                         + (f"('REFER' if adjusted >= {ra!r} else 'DECLINE')" if ra is not None else "'DECLINE'"))
        else:
            lines.append(f"{p}decision = {d['mode'].upper()!r}")
        lines.append(f"{p}return {{'node': {n['id']!r}, 'decision': decision, 'adjusted_score': adjusted, "
                     f"'reason_codes': {list(n.get('reason_codes') or [])!r}, 'pricing_tier': {n.get('pricing_tier') or ''!r}, "
                     f"'limit_multiplier': {_num(n.get('limit_multiplier')) if _num(n.get('limit_multiplier')) is not None else 1.0!r}}}")

    rec(tree, 1)
    return "\n".join(lines) + "\n"


EXPORT_FORMATS = {
    "interconnect": ("Equifax InterConnect (illustrative)", "json"),
    "fico": ("FICO Platform decision tree (mock)", "json"),
    "powercurve": ("Experian PowerCurve Strategy Management (illustrative)", "json"),
    "generic": ("Generic strategy JSON", "json"),
    "sql": ("SQL CASE expressions", "sql"),
    "python": ("Python function", "py"),
}


def export(fmt, tree, meta, column_types=None, backtest_summary=None):
    if fmt == "interconnect":
        return json.dumps(to_interconnect(tree, meta, column_types, backtest_summary), indent=2)
    if fmt == "fico":
        return json.dumps(to_fico(tree, meta), indent=2)
    if fmt == "powercurve":
        return json.dumps(to_powercurve(tree, meta, column_types, backtest_summary), indent=2)
    if fmt == "generic":
        return json.dumps(to_generic(tree, meta), indent=2)
    if fmt == "sql":
        return to_sql(tree, meta)
    if fmt == "python":
        return to_python(tree, meta)
    if _examples is not None and fmt in _examples.FORMATS:
        return _examples.export(fmt, tree, meta, column_types, backtest_summary)
    raise StrategyError(f"Unknown export format '{fmt}'.")


def tree_from_any(obj):
    """Accept a generic export, an InterConnect or PowerCurve export (rebuilt) or a bare tree."""
    if isinstance(obj, dict) and obj.get("type") == "dataiku_strategy_tree":
        return obj["tree"]
    if isinstance(obj, dict) and obj.get("artifactType") == "DECISION_STRATEGY":
        return _tree_from_interconnect(obj)
    if isinstance(obj, dict) and obj.get("artifactType") == "POWERCURVE_STRATEGY_PACKAGE":
        return _tree_from_powercurve(obj)
    if isinstance(obj, dict) and obj.get("type") in ("split", "leaf"):
        return obj
    raise StrategyError("Unrecognised strategy JSON (expected generic, InterConnect, PowerCurve or a tree).")


def _tree_from_interconnect(obj):
    inv = {v: k for k, v in IC_OPERATORS.items()}
    by_id = {n["nodeId"]: n for n in obj["decisionFlow"]["nodes"]}

    def build(nid):
        n = by_id[nid]
        if n["type"] == "CONDITION":
            c, op = n["condition"], inv[n["condition"]["operator"]]
            val, val2 = c.get("value"), None
            if op == "between":
                val, val2 = val[0], val[1]
            elif op in ("in", "not_in"):
                val = ", ".join(val)
            return {"id": nid, "type": "split", "name": n.get("label"), "variable": c["attribute"], "op": op,
                    "value": val, "value2": val2, "missing": "yes" if n.get("missingValueRoute") == "TRUE" else "no",
                    "yes": build(n["onTrue"]), "no": build(n["onFalse"])}
        leaf = new_leaf(nid, n.get("label"))
        for a in n.get("actions", []):
            if a["type"] == "ADJUST_SCORE":
                leaf["score_adjust"] = {"type": {"PERCENT": "pct", "POINTS": "points", "SET": "set"}[a["method"]], "value": a["value"]}
            elif a["type"] == "SET_DECISION" and a["method"] == "SCORE_CUTOFF":
                leaf["decision"] = {"mode": "cutoff", "approve_at": a.get("approveAtOrAbove"), "refer_at": a.get("referAtOrAbove")}
            elif a["type"] == "SET_DECISION":
                leaf["decision"] = {"mode": a["decision"].lower(), "approve_at": None, "refer_at": None}
            elif a["type"] == "SET_REASON_CODES":
                leaf["reason_codes"] = a["codes"]
            elif a["type"] == "SET_PRICING_TIER":
                leaf["pricing_tier"] = a["tier"]
            elif a["type"] == "SET_CREDIT_LIMIT_MULTIPLIER":
                leaf["limit_multiplier"] = a["value"]
        return leaf

    return build(obj["decisionFlow"]["startNode"])


def _now():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def safe_name(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "")).strip("_.").upper()


def column_profile(df, max_top=12):
    """Per-column hints for the node editor."""
    out = []
    for c in df.columns:
        col = df[c]
        numeric = pd.api.types.is_numeric_dtype(col) and not pd.api.types.is_bool_dtype(col)
        info = dict(name=c, kind="numeric" if numeric else "categorical",
                    missing_pct=round(float(col.isna().mean()) * 100, 1), n_unique=int(col.nunique()))
        if numeric and col.notna().any():
            info.update(min=float(col.min()), max=float(col.max()), median=float(col.median()))
            if info["n_unique"] <= max_top:
                info["top"] = [_canon(v) for v in col.dropna().value_counts().index[:max_top]]
        else:
            info["top"] = [_canon(v) for v in col.dropna().astype(str).value_counts().index[:max_top]]
        out.append(info)
    return out


# Example exports for other decisioning engines (Oscilar, Pega, Provenir, Taktile, DMN) live in
# engine_examples; Strategy Studio keeps working without them if that module fails to import.
try:
    import engine_examples as _examples
    EXPORT_FORMATS.update(_examples.FORMATS)
except Exception:  # noqa: BLE001
    _examples = None
