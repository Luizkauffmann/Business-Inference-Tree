import sys, json, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st
df = pd.read_csv(sys.argv[2]); T = "bad_12m_flag_demo"; y = st.target_array(df, T)
tree = st.starter_tree("policy_score_existing", 700)
st.apply_split(tree, tree["id"], {"kind": "numeric", "variable": "vantage_score", "cuts": [567.5], "missing_branch": 0})
k = tree["children"][1]["id"]
st.apply_split(tree, k, {"kind": "numeric", "variable": "debt_to_income_pct", "cuts": [20.95, 24.15, 48.25], "missing_branch": 3})
res, masks, env = st.evaluate(df, tree, "policy_score_existing")
met = st.node_metrics(df, tree, res, masks, env, T)
# 1) raw leaf PD == observed leaf bad rate (credibility 0)
tree["pd_settings"] = {"credibility": 0}
r0, info = st.predict(res.copy(), tree, masks, y, "demo")
ok = all(abs(info["leaves"][l["id"]]["pd"] - met[l["id"]]["bad_rate"]) < 1e-12 for l in st.leaves(tree))
print("raw leaf PD = node bad rate:", ok, {l["id"]: round(met[l["id"]]["bad_rate"], 4) for l in st.leaves(tree)})
# 2) credibility 30 shrinks toward parent; interval present
tree["pd_settings"] = {"credibility": 30}
r1, info = st.predict(res.copy(), tree, masks, y, "demo")
for l in st.leaves(tree):
    v = info["leaves"][l["id"]]; print(f"  {l['id']:4} n_known={v['n_known']:5} raw={v['raw']:.4f} pd={v['pd']:.4f} CI=({v['lower']:.4f},{v['upper']:.4f})")
print("columns:", [c for c in r1.columns if c.startswith("tree_p")], "| state", info["state"], "| thr", round(info["threshold"], 4))
# 3) freeze with 30% holdout, then structure change -> stale
pm = st.freeze_pd(dict(tree, pd_settings={"credibility": 30, "holdout_pct": 30}), masks, y, "demo", T)
tree["pd_model"] = pm; print("state after freeze:", st.pd_state(tree), "| n_known dev:", pm["n_known"])
rf, info = st.predict(res.copy(), tree, masks, None, "other")          # new population: no target needed
print("frozen scoring w/o target:", rf["tree_proba_1"].notna().all(), info["source"][:30])
cal = st.calibration(rf, y, tree, st.holdout_mask(len(df), 30, 42))
print("calibration on holdout: n", cal["n_known"], "AUC %.3f Gini %.3f KS %.3f Brier %.4f" % (cal["auc"], cal["gini"], cal["ks"], cal["brier"]))
for l in cal["leaves"]: print("   ", l["leaf"], "pred %.4f obs %.4f inside CI %s" % (l["predicted"], l["observed"], l["inside"]))
# 4) exports reproduce frozen PD + prediction
con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
q = pd.read_sql(st.to_sql(tree, {"name": "T", "score_col": "policy_score_existing"}), con)
print("SQL proba agreement:", np.allclose(q["tree_proba_1"].values, rf["tree_proba_1"].values), "prediction:", (q["tree_prediction"].values == rf["tree_prediction"].values).all())
ns = {}; exec(st.to_python(tree, {"name": "T", "score_col": "policy_score_existing"}), ns)
py = [ns["decide"]({k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in df.to_dict("records")]
print("Python proba agreement:", np.allclose([d["proba_1"] for d in py], rf["tree_proba_1"].values), "prediction:", all(d["prediction"] == p for d, p in zip(py, rf["tree_prediction"].values)))
tree["children"][1]["split"]["cuts"][0] = 21.0
print("state after editing a cut:", st.pd_state(tree))
