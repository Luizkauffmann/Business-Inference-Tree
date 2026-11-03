"""Multi-output leaves: default outputs, leaf context (leaf_score / leaf_decision / leaf_event_rate),
decision from an output, segment-only leaves – app vs SQL vs Python, live and frozen rates."""
import sys, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st
df = pd.read_csv(sys.argv[2]); S = "policy_score_existing"; y = st.target_array(df, "bad_12m_flag_demo")
def leaf(i, name, **k):
    n = st.new_leaf(i, name); n["leaf"].update(k); return n
tree = {"id": "n1", "name": "All", "calcs": [{"name": "adj_score", "expr": "policy_score_existing - 15 * inquiries_6m_count"}],
        "default_outputs": [
            {"name": "risk_grade", "expr": "'A' if leaf_event_rate < 0.03 else ('B' if leaf_event_rate < 0.06 else 'C')"},
            {"name": "apr", "expr": "round(0.099 + 1.2 * leaf_event_rate, 4)"},
            {"name": "max_amount", "expr": "0 if leaf_decision == 'DECLINE' else round(stated_monthly_income * 3, 0)"}],
        "split": {"kind": "numeric", "variable": "vantage_score", "cuts": [620, 700], "missing_branch": 0},
        "children": [
            leaf("n2", "Low", mode="cutoff", score_expr="adj_score", approve_at=820, refer_at=780, reason_codes=["LOW"]),
            leaf("n3", "Mid", mode="output", decision_output="credit_decision",
                 outputs=[{"name": "credit_decision", "expr": "'APPROVE' if leaf_event_rate < 0.05 and leaf_score >= 760 else ('REFER' if leaf_event_rate < 0.08 else 'DECLINE')"},
                          {"name": "apr", "expr": "0.149 if credit_decision == 'APPROVE' else None"}],
                 limit_expr="1.1 if leaf_decision == 'APPROVE' else 0"),
            leaf("n4", "High", mode="none", outputs=[{"name": "segment", "expr": "'PRIME' if leaf_score >= 800 else leaf_id"}])]}
iss = st.validate(tree, df.columns, S); print("validation errors:", [i for i in iss if i["level"] == "error"] or "none")
bad = dict(tree, children=[tree["children"][0], dict(tree["children"][1], leaf=dict(tree["children"][1]["leaf"], decision_output="nope"))] + tree["children"][2:])
print("bad decision_output caught:", any("nope" in i["message"] for i in st.validate(bad, df.columns, S)))
con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
for nm, f in (("GREATEST", max), ("LEAST", min)): con.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
def check(tag, t, rates=None):
    res, masks, _ = st.evaluate_full(df, t, S, y, "demo")
    print(f"[{tag}] uses rates:", st.uses_leaf_rates(t), "| decisions:", res["tree_decision"].fillna("<none>").value_counts().to_dict())
    print(f"[{tag}] summary no_decision_rate: {st.summary(res, df)['no_decision_rate']:.3f}")
    print(res.groupby("tree_leaf")[["out_risk_grade", "out_apr", "out_max_amount", "out_credit_decision", "out_segment", "limit_multiplier"]].first().to_string())
    q = pd.read_sql(st.to_sql(t, {"name": "T", "score_col": S}, leaf_rates=rates), con)
    ns = {}; exec(st.to_python(t, {"name": "T", "score_col": S}, leaf_rates=rates), ns)
    py = [ns["decide"]({k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in df.to_dict("records")]
    norm = lambda v: None if v is None or (isinstance(v, float) and np.isnan(v)) else (round(float(v), 6) if isinstance(v, (int, float, np.floating)) and not isinstance(v, bool) else str(v))
    for col in ["tree_decision", "limit_multiplier", "out_risk_grade", "out_apr", "out_max_amount", "out_credit_decision", "out_segment"]:
        a = [norm(v) for v in res[col]]
        sq = [norm(v) for v in q[col]]
        key = col[4:] if col.startswith("out_") else {"tree_decision": "decision", "limit_multiplier": "limit_multiplier"}[col]
        pv = [norm(d["outputs"].get(key) if col.startswith("out_") else d[key]) for d in py]
        print(f"   {col:22} SQL {np.mean([x == z for x, z in zip(a, sq)]):.4f}  Python {np.mean([x == z for x, z in zip(a, pv)]):.4f}")
# live rates (exports given the same live table as the app)
_, m, _ = st.evaluate(df, tree, S)
check("live", tree, st.rate_table(tree, m, y, "demo")[0])
# frozen rates (exports read them from the tree)
t2 = dict(tree); t2["pd_model"] = st.freeze_pd(t2, m, y, "demo", "bad_12m_flag_demo")
check("frozen", t2)
print("studio issues:", st.to_studio(tree, {"score_col": S})[1])
_, m, _ = st.evaluate(df, tree, S); rates = st.rate_table(tree, m, y, "demo")[0]
res, _, _ = st.evaluate_full(df, tree, S, y, "demo"); q = pd.read_sql(st.to_sql(tree, {"name": "T", "score_col": S}, leaf_rates=rates), con)
d = pd.DataFrame({"leaf": res.tree_leaf, "dec": res.tree_decision, "app": res.out_max_amount, "sql": q.out_max_amount, "inc": df.stated_monthly_income})
bad_ = d[[not (pd.isna(a) and pd.isna(b)) and (pd.isna(a) or pd.isna(b) or abs(float(a) - float(b)) > 1e-6) for a, b in zip(d.app, d.sql)]]
print(bad_.head(8).to_string()); print(bad_.leaf.value_counts().to_dict(), bad_.dec.value_counts(dropna=False).to_dict())
