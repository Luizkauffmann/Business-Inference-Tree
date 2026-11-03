"""DP optimal binning vs optbinning (when installed) on the same node."""
import sys, time, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st
df = pd.read_csv(sys.argv[2]); y = st.target_array(df, "bad_12m_flag_demo")
known = ~np.isnan(y)
def iv(split, x):
    p = {"id": "p", "split": split, "children": [st.new_leaf(f"c{i}") for i in range(st.n_branches(split))]}
    env = st.Env(df); m = np.ones(len(df), bool)
    return st.branch_stats(split, env, df.index, m, y)["iv"]
for var, kind in [("vantage_score", None), ("debt_to_income_pct", None), ("revolving_utilization_pct", None), ("state", None)]:
    x = df[var]
    t = time.time(); sp = st._dp_splits(x[known], y[known].astype(int), kind != "categorical" and pd.api.types.is_numeric_dtype(x), 4, 0.05, "auto"); dt = time.time() - t
    full = st.suggest_split(x, y, max_bins=4, min_share=0.05)
    print(f"{var:26} DP: {str(sp)[:70]:72} {dt:.2f}s | via suggest_split: {full['method'][:12]} IV {iv(full, x):.3f}")
