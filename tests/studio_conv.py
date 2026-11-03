import sys, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st
df = pd.read_csv(sys.argv[2]); y = st.target_array(df, "bad_12m_flag_demo")
def canon(z): return st._canon(z)
def studio_eval(node, row):  # Strategy Studio semantics (strategy_engine._condition_mask)
    while node["type"] == "split":
        v = row[node["variable"]]; miss = v is None or (isinstance(v, float) and np.isnan(v))
        if node["op"] == "is_missing": yes = miss
        elif miss: yes = node["missing"] == "yes"
        elif node["op"] == "<": yes = float(v) < float(node["value"])
        elif node["op"] == "in": yes = canon(v) in {canon(x.strip()) for x in str(node["value"]).split(",")}
        node = node["yes"] if yes else node["no"]
    d = node["decision"]
    if d["mode"] != "cutoff": return d["mode"].upper()
    s = row["policy_score_existing"]
    return "APPROVE" if s >= d["approve_at"] else ("REFER" if d["refer_at"] is not None and s >= d["refer_at"] else "DECLINE")
t = st.starter_tree("policy_score_existing", 800)
t = st.apply_split(t, "n1", st.suggest_split(df["vantage_score"], y, max_bins=4, missing="separate"))
sp = st.suggest_split(df["state"], y, max_bins=3); t = st.apply_split(t, t["children"][1]["id"], sp)
sp2 = st.suggest_split(df["ltv_pct"], y, max_bins=3, missing="worst"); t = st.apply_split(t, t["children"][2]["id"], sp2)
for i, lf in enumerate(st.leaves(t)):
    lf["leaf"]["approve_at"] = 780 + 10 * (i % 4); lf["leaf"]["refer_at"] = 760
print("branches:", [st.n_branches(n["split"]) for n, _, _ in st.walk(t) if n.get("split")], "missing:", [(n["split"].get("missing_separate"), n["split"].get("missing_branch")) for n, _, _ in st.walk(t) if n.get("split")])
res, _, _ = st.evaluate(df, t)
stree, issues = st.to_studio(t, {"score_col": "policy_score_existing"})
recs = df.where(df.notna(), None).to_dict("records")
agree = np.mean([studio_eval(stree, r) == d for r, d in zip(recs, res["tree_decision"])])
print("issues:", issues, "| Studio conversion agreement:", agree)
t = st.starter_tree("policy_score_existing", 800)
t = st.apply_split(t, "n1", {"kind": "numeric", "variable": "ltv_pct", "cuts": [80, 100], "missing_separate": True})
t = st.apply_split(t, t["children"][3]["id"], {"kind": "categorical", "variable": "state", "groups": [["CA", "NY"], ["TX"]], "other_separate": True, "missing_branch": 0})
for i, lf in enumerate(st.leaves(t)): lf["leaf"]["approve_at"] = 770 + 15 * i
res, _, _ = st.evaluate(df, t); stree, issues = st.to_studio(t, {"score_col": "policy_score_existing"})
print("missing rows ltv:", int(df["ltv_pct"].isna().sum()), "| separate-missing + other branch agreement:",
      np.mean([studio_eval(stree, r) == d for r, d in zip(recs, res["tree_decision"])]), issues)
