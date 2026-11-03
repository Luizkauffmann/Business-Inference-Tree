"""Backtest SQL per dialect: parsed by sqlglot as each warehouse, executed on SQLite (backticks too)."""
import sys, sqlite3, numpy as np, pandas as pd, sqlglot
sys.path.insert(0, sys.argv[1]); import strategy_tree as st, tree_expr as tx
df = pd.read_csv(sys.argv[2]); S = "policy_score_existing"; T = "bad_12m_flag_demo"; y = st.target_array(df, T)
exec(open(sys.argv[3]).read().split("iss = st.validate")[0].split("tree = ", 1)[0])      # leaf() helper
tree = eval(open(sys.argv[3]).read().split("tree = ", 1)[1].split("\niss = ")[0])          # multi-output tree
tree["children"][0]["leaf"]["reason_codes"] = ["O'BRIEN_RULE"]                              # a quote inside a literal
_, m, _ = st.evaluate(df, tree, S); tree["pd_model"] = st.freeze_pd(tree, m, y, "demo", T)
con = sqlite3.connect(":memory:"); df.to_sql("apps", con, index=False)
for nm, f in (("GREATEST", max), ("LEAST", min)): con.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
res, masks, _ = st.evaluate_full(df, tree, S, y, "demo"); res, _ = st.predict(res, tree, masks, y, "demo")
mem = st.backtest_summary(st.backtest_groups(res, df, T), 350, 4200)
for dialect, table, readers in [("ansi", '"apps"', ["snowflake", "postgres", "redshift", "duckdb"]),
                                ("bigquery", "`proj.dataset.apps`", ["bigquery"]), ("mysql", "`apps`", ["mysql"])]:
    q = st.backtest_sql(tree, {"name": "T", "score_col": S}, table, T, dialect=dialect)
    ok = []
    for r in readers:
        try:
            sqlglot.parse_one(q, read=r); ok.append(r + " ✓")
        except Exception as e:
            ok.append(f"{r} ✗ {str(e)[:120]}")
    bad_dq = dialect != "ansi" and '"' in q.replace('\\"', "")
    line = f"{dialect:9} parse: {', '.join(ok)} | double quotes left: {bad_dq}"
    if dialect != "bigquery":   # SQLite understands "..." and `...` identifiers, not BigQuery's \\' escape
        db = st.backtest_summary(pd.read_sql(q.replace("`proj.dataset.apps`", "`apps`"), con), 350, 4200)
        same = all((mem[k] is None and db[k] is None) or abs((mem[k] or 0) - (db[k] or 0)) < 1e-9 for k in mem)
        line += f" | executed: KPIs equal in-memory {same}"
    print(line)
print("bigquery literal:", [l for l in st.backtest_sql(tree, {"name": "T", "score_col": S}, "`t`", T, dialect="bigquery").split("\n") if "BRIEN" in l][0].strip()[:90])
# numeric categorical split: typed literals in the backtest SQL, unchanged export
t2 = {"id": "n1", "name": "All", "calcs": [], "leaf": None,
      "split": {"kind": "categorical", "variable": "delinquencies_24m_count", "groups": [["0"], ["1", "2"]], "other_branch": 1, "missing_branch": 0},
      "children": [st.new_leaf("n2", "a"), st.new_leaf("n3", "b")]}
for c in t2["children"]: c["leaf"].update(mode="approve")
nums = [k for k in df.columns if pd.api.types.is_numeric_dtype(df[k])]
qb = st.backtest_sql(t2, {"name": "T", "score_col": S}, "`t`", T, dialect="bigquery", numeric_columns=nums)
print("typed literals:", [l.strip()[:80] for l in qb.split("\n") if "IN (" in l][0])
print("export unchanged:", "IN ('0')" in st.to_sql(t2, {"name": "T", "score_col": S}))
q = st.backtest_sql(t2, {"name": "T", "score_col": S}, '"apps"', T, numeric_columns=nums)
r2, _, _ = st.evaluate(df, t2, S)
print("numeric categories executed = in-memory:", st.backtest_summary(pd.read_sql(q, con))["approval_rate"] == st.backtest_summary(st.backtest_groups(r2, df, T))["approval_rate"],
      "| leaf shares:", pd.read_sql(q, con).groupby("tree_leaf")["n"].sum().to_dict(), r2["tree_leaf"].value_counts().to_dict())
