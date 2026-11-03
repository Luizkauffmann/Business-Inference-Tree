"""Visual ML import on the UCI sample (numbers quoted in docs/visual-ml-import.md).

Emulates what Dataiku's Lab does (mean imputation, AVGSTD rescaling, dummy-encoded categories, a depth-4 tree on
the development half), builds the raw tree structure Dataiku returns, imports it with from_dss_trees and checks it.
Usage: python tests/uci_vml_example.py <lib dir> docs/demo/data/credit_card_default.csv
"""
import sys, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1])
import strategy_tree as st, strategy_engine as se
from sklearn.tree import DecisionTreeClassifier

df = pd.read_csv(sys.argv[2])
T = "default_next_month"
num = ["pay_status_m1", "pay_status_m2", "max_delinquency_6m", "months_late_6m", "utilization_m1",
       "avg_utilization_6m", "payment_ratio_m1", "credit_limit"]
dev = (df["sample"] == "development").values
X = df[num].fillna(df.loc[dev, num].mean())
Z = (X - X[dev].mean()) / X[dev].std(ddof=1)                               # AVGSTD, fitted on the training rows
Z = pd.concat([Z, pd.get_dummies(df["education"], prefix="dummy:education", prefix_sep=":").astype(float)], axis=1)
clf = DecisionTreeClassifier(max_depth=4, min_samples_leaf=100, random_state=0).fit(Z.values[dev], df.loc[dev, T])
tr = clf.tree_
raw = {"featureNames": list(Z.columns), "classes": ["0", "1"], "trees": [{
    "leftChild": tr.children_left.tolist(), "rightChild": tr.children_right.tolist(), "feature": tr.feature.tolist(),
    "threshold": tr.threshold.tolist(), "nSamples": tr.n_node_samples.tolist(),
    "probas": (tr.value[:, 0, :] / tr.value[:, 0, :].sum(1, keepdims=True)).tolist()}]}

THR = float(sys.argv[3]) if len(sys.argv) > 3 else 0.8
d_dev = df[dev].reset_index(drop=True)
imp, rep = st.from_dss_trees(raw, d_dev, approve_class="0", threshold=THR)
print("leaves", rep["leaves"], "| max share gap", round(rep["max_share_gap"], 4))
for n in rep["notes"][:4]:
    print("  ", n)
root = imp["split"]
print("root split:", root["variable"], root.get("cuts"))
for k in imp["children"]:
    print("   child", k["name"], "->", k["split"]["variable"] if k.get("split") else "leaf")

val = df[~dev].reset_index(drop=True)
res, _, _ = st.evaluate(val, imp)
p0 = clf.predict_proba(Z.values[~dev])[:, 0]
agree = np.mean(res["tree_decision"].values == np.where(p0 >= THR, "APPROVE", "DECLINE"))
app = res["tree_decision"].values == "APPROVE"
y = val[T].values
print(f"validation: rows {len(val)}, agreement with the model {agree:.4f}, approval {app.mean():.3f}, "
      f"event rate approved {y[app].mean():.3f}, declined {y[~app].mean():.3f}, overall {y.mean():.3f}")
assert agree == 1.0 and rep["max_share_gap"] < 0.01
stree, issues = st.to_studio(imp, {"score_col": "behaviour_score"})
sres, _ = se.evaluate(val, stree, "behaviour_score")
same = np.mean(sres["strategy_decision"].values == res["tree_decision"].values)
print(f"send to studio: {len(issues)} issues (all 'model_p_0' outputs: {all('model_p_0' in i for i in issues)}), "
      f"Strategy Studio agreement {same:.4f}")
assert same == 1.0
