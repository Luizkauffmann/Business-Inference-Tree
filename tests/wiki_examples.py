import sys, sqlite3, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1]); import strategy_tree as st
df = pd.read_csv(sys.argv[2]); S = "policy_score_existing"
L = lambda i, name, **k: dict(st.new_leaf(i, name), leaf=dict(st.new_leaf(i)["leaf"], **k))
tree = {"id": "n1", "name": "All applications", "calcs": [
          {"name": "adj_score", "expr": "policy_score_existing - 15 * inquiries_6m_count"},
          {"name": "dti_stress", "expr": "debt_to_income_pct * 1.1 + 5"},
          {"name": "loan_to_income", "expr": "requested_amount / max(stated_monthly_income * 12, 1)"}],
        "split": {"kind": "rule", "rules": ["delinquencies_24m_count > 0 or collections_count > 0",
                                             "thin_file_flag == 1 and member_tenure_years < 2"]},
        "children": [
          L("n2", "Derogatory", mode="decline", reason_codes=["DELINQ_OR_COLLECTION"]),
          L("n3", "Thin file, new member", mode="refer", reason_codes=["THIN_FILE"]),
          {"id": "n4", "name": "Clean file", "calcs": [], "leaf": None,
           "split": {"kind": "numeric", "variable": "dti_stress", "cuts": [35, 50], "missing_branch": 2},
           "children": [
             L("n5", "Low DTI", mode="cutoff", score_expr="adj_score", approve_at=760, refer_at=740, pricing_tier="A",
               limit_expr="clip(1.5 - dti_stress / 100, 0.5, 1.2)",
               outputs=[{"name": "apr", "expr": "0.199 if vantage_score < 640 else 0.149"},
                        {"name": "max_amount", "expr": "round(stated_monthly_income * 3, 0)"}]),
             L("n6", "Mid DTI", mode="cutoff", score_expr="where(revolving_utilization_pct > 80, adj_score - 20, adj_score)",
               approve_at=780, refer_at=760, pricing_tier="B", reason_codes=["HIGH_UTIL"], limit_expr="0.8"),
             L("n7", "High DTI", mode="cutoff", score_expr="adj_score - 30", approve_at=800, pricing_tier="C",
               reason_codes=["HIGH_DTI"], limit_expr="0.6 if loan_to_income > 0.3 else 0.8")]}]}
iss = st.validate(tree, df.columns, S); print("issues:", [i for i in iss if i["level"] == "error"] or "none")
res, masks, env = st.evaluate(df, tree, S)
print(res["tree_leaf"].value_counts().to_dict(), res["tree_decision"].value_counts().to_dict())
print(res[["tree_leaf", "tree_score", "tree_decision", "limit_multiplier", "out_apr", "out_max_amount", "calc_adj_score", "calc_dti_stress"]].dropna().head(3).to_string())
con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
for nm, f in (("GREATEST", max), ("LEAST", min)): con.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
q = pd.read_sql(st.to_sql(tree, {"name": "W", "score_col": S}), con)
print("SQL decision agreement:", (q["tree_decision"].values == res["tree_decision"].values).mean())
ns = {}; exec(st.to_python(tree, {"name": "W", "score_col": S}), ns)
py = [ns["decide"]({k: (None if pd.isna(v) else v) for k, v in r.items()})["decision"] for r in df.to_dict("records")]
print("Python decision agreement:", (np.array(py) == res["tree_decision"].values).mean())
