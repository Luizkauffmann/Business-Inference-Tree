"""Builds the trees of the wiki article 'Labels – risk examples' and checks they validate, evaluate and
export (SQL / Python / Strategy Studio) with identical results."""
import sys, sqlite3, numpy as np, pandas as pd
sys.path[:0] = [sys.argv[1]]
import strategy_tree as st, strategy_engine as se
df = pd.read_csv(sys.argv[2]); S = "policy_score_existing"; T = "bad_12m_flag_demo"; y = st.target_array(df, T)
def L(i, name, labels=None, **k):
    n = st.new_leaf(i, name); n["leaf"].update(k)
    if labels: n["labels"] = labels
    return n
def split(i, name, sp, kids, labels=None, calcs=None):
    n = {"id": i, "name": name, "calcs": calcs or [], "leaf": None, "split": sp, "children": kids}
    if labels: n["labels"] = labels
    return n
RATING = {"name": "Rating", "values": ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]}
# Example 1 – risk rating grid: score bands, then DTI inside the middle bands
ex1 = split("n1", "All applications", {"kind": "numeric", "variable": "vantage_score", "cuts": [643.5, 686.5, 792.5], "missing_branch": 0}, [
    L("n2", "Score < 643.5 + missing", {"Rating": "CCC"}, mode="decline", reason_codes=["LOW_SCORE"]),
    split("n3", "Score [643.5, 686.5)", {"kind": "numeric", "variable": "debt_to_income_pct", "cuts": [36, 45], "missing_branch": 2}, [
        L("n6", "DTI < 36", {"Rating": "BBB"}, approve_at=760), L("n7", "DTI [36, 45)", approve_at=780), L("n8", "DTI ≥ 45 + missing", {"Rating": "B"}, mode="refer", reason_codes=["HIGH_DTI"])],
        labels={"Rating": "BB"}),
    split("n4", "Score [686.5, 792.5)", {"kind": "numeric", "variable": "debt_to_income_pct", "cuts": [36], "missing_branch": 1}, [
        L("n9", "DTI < 36", {"Rating": "A"}, approve_at=740), L("n10", "DTI ≥ 36 + missing", approve_at=760)], labels={"Rating": "BBB"}),
    L("n5", "Score ≥ 792.5", {"Rating": "AAA"}, mode="approve")])
ex1["label_defs"] = [RATING]
# Example 2 – pricing tier labels drive APR and limit for every leaf (root default outputs read the label)
ex2 = st.copy.deepcopy(ex1)
ex2["label_defs"] = [RATING, {"name": "Pricing_tier", "values": ["P1", "P2", "P3", "P4"]}]
for nid, tier in {"n5": "P1", "n4": "P2", "n9": "P1", "n3": "P3", "n2": "P4"}.items():
    st.find(ex2, nid)[0].setdefault("labels", {})["Pricing_tier"] = tier
ex2["default_outputs"] = [
    {"name": "apr", "expr": "0.099 if label_Pricing_tier == 'P1' else (0.129 if label_Pricing_tier == 'P2' else (0.169 if label_Pricing_tier == 'P3' else 0.219))"},
    {"name": "max_amount", "expr": "0 if leaf_decision == 'DECLINE' else round(stated_monthly_income * (4 if label_Pricing_tier in ['P1', 'P2'] else 2), 0)"}]
# Example 3 – policy segments with a rule split; decision from an output that reads the segment
ex3 = split("n1", "All applications", {"kind": "rule", "rules": [
        "delinquencies_24m_count > 0 or collections_count > 0",
        "thin_file_flag == 1",
        "vantage_score >= 760 and debt_to_income_pct < 36"]}, [
    L("n2", "Derogatory", {"Segment": "DEROG"}, mode="decline", reason_codes=["DELINQ_OR_COLLECTION"]),
    L("n3", "Thin file", {"Segment": "THIN_FILE"}, mode="output", decision_output="credit_decision",
      outputs=[{"name": "credit_decision", "expr": "'APPROVE' if member_tenure_years >= 3 and leaf_event_rate < 0.08 else 'REFER'"}]),
    L("n4", "Prime", {"Segment": "PRIME"}, mode="approve"),
    L("n5", "Otherwise", {"Segment": "NEAR_PRIME"}, approve_at=780)])
ex3["label_defs"] = [{"name": "Segment", "values": ["PRIME", "NEAR_PRIME", "THIN_FILE", "DEROG"]}]
# Example 4 – test cells for a champion / challenger experiment, tracked with a label
ex4 = st.copy.deepcopy(ex1)
ex4["label_defs"] = [RATING, {"name": "Test_cell", "values": ["CHAMPION", "TEST_A"]}]
ex4["labels"] = {"Test_cell": "CHAMPION"}
st.find(ex4, "n3")[0]["labels"]["Test_cell"] = "TEST_A"          # test a lower cut-off in the 643.5–686.5 band
st.find(ex4, "n6")[0]["leaf"]["approve_at"] = 740
st.find(ex4, "n7")[0]["leaf"]["approve_at"] = 760
con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
for nm, f in (("GREATEST", max), ("LEAST", min)): con.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
for name, t in [("ex1 rating grid", ex1), ("ex2 pricing tiers", ex2), ("ex3 policy segments", ex3), ("ex4 test cells", ex4)]:
    errs = [i["message"] for i in st.validate(t, df.columns, S) if i["level"] == "error"]
    res, m, _ = st.evaluate_full(df, t, S, y, "demo")
    q = pd.read_sql(st.to_sql(t, {"name": name, "score_col": S}, leaf_rates=st.rate_table(t, m, y, "demo")[0]), con)
    cols = [c for c in res.columns if c.startswith(("label_", "out_"))] + ["tree_decision"]
    def same(a, b):
        na = a is None or (isinstance(a, float) and np.isnan(a)); nb = b is None or (isinstance(b, float) and np.isnan(b))
        if na or nb:
            return na and nb
        if isinstance(a, str) or isinstance(b, str):
            return str(a) == str(b)
        return abs(float(a) - float(b)) < 1e-9
    agree = {c: float(np.mean([same(a, b) for a, b in zip(res[c].tolist(), q[c].tolist())])) for c in cols}
    studio, _ = st.to_studio(t, {"score_col": S}); sres, _ = se.evaluate(df, studio, S)
    lab = [c for c in res.columns if c.startswith("label_")]
    same_studio = all((sres[c].astype(str).values == res[c].astype(str).values).all() for c in lab)
    dist = {c: res[c].value_counts(dropna=False).to_dict() for c in lab}
    if "label_Rating" in res and name.startswith("ex1"):
        g = pd.DataFrame({"r": res["label_Rating"], "y": y, "d": res["tree_decision"]})
        order = ["AAA", "AA", "A", "BBB", "BB", "B", "CCC"]
        print("   event rate by rating:", {k: round(float(v), 4) for k, v in g.groupby("r")["y"].mean().reindex(order).dropna().items()},
              "| share:", {k: round(float(v), 3) for k, v in g["r"].value_counts(normalize=True).reindex(order).dropna().items()},
              "| approval:", {k: round(float(v), 3) for k, v in g.groupby("r")["d"].apply(lambda d: (d == "APPROVE").mean()).reindex(order).dropna().items()})
    if "label_Segment" in res:
        g = pd.DataFrame({"r": res["label_Segment"], "y": y, "d": res["tree_decision"]})
        print("   by segment:", {k: (round(float(g[g.r == k].y.mean()), 4), round(float((g[g.r == k].d == "APPROVE").mean()), 3)) for k in g.r.unique()})
    print(f"{name}: errors {errs or 'none'} | SQL agreement {min(agree.values()):.4f} {agree if min(agree.values()) < 1 else ''} | Studio labels identical {same_studio}\n   {dist}")
    if min(agree.values()) < 1:
        c = min(agree, key=agree.get); print("   sample", c, list(zip(res[c].tolist()[:3], q[c].tolist()[:3])))
    if "out_apr" in res:
        print("   apr by tier:", res.groupby("label_Pricing_tier")["out_apr"].first().to_dict(), "| max_amount>0 share:", round(float((res.out_max_amount.astype(float) > 0).mean()), 3))
    if name.startswith("ex4"):
        g = pd.DataFrame({"cell": res["label_Test_cell"], "dec": res["tree_decision"], "y": y})
        print("   approval by cell:", g.groupby("cell")["dec"].apply(lambda d: round(float((d == "APPROVE").mean()), 3)).to_dict(),
              "| event rate approved by cell:", g[g.dec == "APPROVE"].groupby("cell")["y"].mean().round(4).to_dict())
