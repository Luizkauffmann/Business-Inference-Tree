import sys, time, json, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st, tree_expr as tx
df = pd.read_csv(sys.argv[2]); T="bad_12m_flag_demo"; y = st.target_array(df, T)
print("rows", len(df), "known", int((~np.isnan(y)).sum()), "bad rate", np.nanmean(y))
# expressions
env = st.Env(df)
e = tx.evaluate("where(thin_file_flag == 1, policy_score_existing - 20, policy_score_existing)", env, df.index)
print("expr ok", e.head(3).tolist(), tx.evaluate("state in ['CA','NY'] and not isnull(collateral_value)", env, df.index).mean())
print("sql:", tx.to_sql("600 if delinquencies_24m_count > 0 else min(vantage_score, 700)"))
# tree build
cols = [c for c in df.columns if c not in (T, "bad_latent_synthetic", "bad_12m_flag")]
t0=time.time(); pb = st.Prebin(df, cols, target=T); print("prebin", round(time.time()-t0,2), "s")
tree = st.starter_tree("policy_score_existing", 800)
res, masks, env = st.evaluate(df, tree)
rel = pb.relevance(masks["n1"], y)
print("top relevance:", [(r["variable"], round(r["relevance"],1), round(r["iv"],3), r["flags"]) for r in rel[:8]])
t0=time.time()
sp = st.suggest_split(df["vantage_score"], y, max_bins=4, min_share=0.05, monotonic="auto")
print("optbinning split", sp, round(time.time()-t0,2),"s")
tree = st.apply_split(tree, "n1", sp)
res, masks, env = st.evaluate(df, tree)
print("preview", json.dumps(st.branch_stats(sp, env, df.index, masks["n1"], y), default=float)[:400])
# second multi-split on same variable right after root? split child n2 on state (categorical) and n3 with a rule + calc
kid = tree["children"][0]["id"]
sp2 = st.suggest_split(df["state"], y, max_bins=3)
tree = st.apply_split(tree, kid, sp2)
n3 = tree["children"][1]
n3["calcs"] = [{"name": "adj_score", "expr": "policy_score_existing - 15 * inquiries_6m_count"}]
tree = st.apply_split(tree, n3["id"], {"kind": "rule", "rules": ["debt_to_income_pct > 45 and adj_score < 760", "revolving_utilization_pct > 85"]})
for lf in st.leaves(n3):
    lf["leaf"]["score_expr"] = "adj_score"
print("validate:", st.validate(tree, df.columns))
res, masks, env = st.evaluate(df, tree)
cm = [{"name": "avg_dti", "agg": "mean", "expr": "debt_to_income_pct"}, {"name": "thin_rate", "agg": "rate", "expr": "thin_file_flag == 1"}]
nm = st.node_metrics(df, tree, res, masks, env, T, 350, 4200, cm)
print("root metrics", {k: (round(v,4) if isinstance(v,float) else v) for k,v in nm["n1"].items()})
print("summary", st.summary(res, df, T, 350, 4200))
print("guidance", [(g["name"], g["variable"], round(g["gain"],3)) for g in st.guidance(tree, masks, pb, y)])
# exporters consistency: python export vs engine
code = st.to_python(tree, {"name": "T", "score_col": "policy_score_existing"}); ns = {}; exec(code, ns)
recs = df.where(df.notna(), None).to_dict("records")
agree = np.mean([ns["decide"](r)["decision"] == d for r, d in zip(recs, res["tree_decision"])])
print("python export agreement", agree)
sql = st.to_sql(tree, {"name": "T", "score_col": "policy_score_existing"})
import sqlite3; con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
try:
    q = pd.read_sql(sql.replace("LEAST(","MIN(").replace("GREATEST(","MAX("), con)
    print("sql export agreement", (q["tree_decision"].values == res["tree_decision"].values).mean())
except Exception as ex:
    print("sqlite check skipped:", ex)
stree, issues = st.to_studio(tree, {"score_col": "policy_score_existing"}); print("studio issues", issues[:3])
# Studio conversion of a formula-free tree agrees with Strategy Studio-equivalent routing
t2 = st.apply_split(st.starter_tree("policy_score_existing", 800), "n1", sp)
r2,_,_ = st.evaluate(df, t2); s2, iss2 = st.to_studio(t2, {"score_col": "policy_score_existing"}); print("studio issues (plain tree)", iss2)
# scale: 1500 attributes
wide = pd.DataFrame(np.random.RandomState(0).randn(len(df), 1500).astype(np.float32), columns=[f"attr_{i}" for i in range(1500)])
wide[T] = df[T]
t0=time.time(); pbw = st.Prebin(wide, [c for c in wide.columns if c != T], target=T); t1=time.time()
relw = pbw.relevance(np.ones(len(wide), bool), y); t2=time.time()
print(f"1500 attrs: prebin {t1-t0:.2f}s, relevance {t2-t1:.3f}s, n={len(relw)}")
# Visual ML import: train sklearn tree on standardized data, emulate Dataiku raw tree
from sklearn.tree import DecisionTreeClassifier
feats = ["vantage_score", "debt_to_income_pct", "ltv_pct", "thin_file_flag"]
X = df[feats].fillna(df[feats].mean()); Z = (X - X.mean())/X.std(ddof=1)
d = pd.get_dummies(df["state"], prefix="dummy:state", prefix_sep=":").astype(float)
Z = pd.concat([Z, d], axis=1)
yy = (df["existing_decision"] == "Decline").astype(int)
clf = DecisionTreeClassifier(max_depth=4, random_state=0).fit(Z.values, yy)
tr = clf.tree_
raw = {"featureNames": list(Z.columns), "classes": ["Approve", "Decline"], "trees": [{
  "leftChild": tr.children_left.tolist(), "rightChild": tr.children_right.tolist(), "feature": tr.feature.tolist(),
  "threshold": tr.threshold.tolist(), "nSamples": tr.n_node_samples.tolist(),
  "probas": (tr.value[:,0,:]/tr.value[:,0,:].sum(1,keepdims=True)).tolist()}]}
imp, rep = st.from_dss_trees(raw, df, approve_class="Approve")
ri,_,_ = st.evaluate(df, imp)
pred = np.where(clf.predict_proba(Z.values)[:,0] >= 0.5, "APPROVE", "DECLINE")
print("VML import: agreement with sklearn", (ri["tree_decision"].values == pred).mean(), "max share gap", rep["max_share_gap"], rep["notes"][:2])
