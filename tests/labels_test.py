"""Labels end to end: inheritance, label_<name> columns, label_ in formulas, SQL / Python exports,
Send to Studio, Strategy Studio evaluation and exports (InterConnect, PowerCurve, engine examples, DMN)."""
import sys, json, sqlite3, numpy as np, pandas as pd, xml.etree.ElementTree as ET
sys.path[:0] = [sys.argv[1]]
import strategy_tree as st, strategy_engine as se, engine_examples as ex
df = pd.read_csv(sys.argv[2]); S = "policy_score_existing"; y = st.target_array(df, "bad_12m_flag_demo")
L = lambda i, name, **k: dict(st.new_leaf(i, name), leaf=dict(st.new_leaf(i)["leaf"], **k))
tree = {"id": "n1", "name": "All", "calcs": [], "leaf": None,
        "label_defs": [{"name": "Rating", "values": ["AAA", "AA", "A", "BBB", "BB", "B"]}, {"name": "Pricing_tier", "values": ["P1", "P2", "P3"]}],
        "default_outputs": [{"name": "apr", "expr": "0.099 if label_Rating in ['AAA', 'AA'] else (0.149 if label_Rating == 'A' else 0.199)"}],
        "split": {"kind": "numeric", "variable": "vantage_score", "cuts": [650, 750], "missing_branch": 0},
        "children": [
            L("n2", "Low", mode="cutoff", approve_at=850),
            {"id": "n3", "name": "Mid", "calcs": [], "leaf": None, "labels": {"Rating": "BBB", "Pricing_tier": "P2"},
             "split": {"kind": "numeric", "variable": "debt_to_income_pct", "cuts": [35], "missing_branch": 1},
             "children": [dict(L("n5", "Mid low DTI", mode="cutoff", approve_at=800), labels={"Rating": "A"}),
                          L("n6", "Mid high DTI", mode="cutoff", approve_at=820)]},
            dict(L("n4", "High", mode="approve"), labels={"Rating": "AAA", "Pricing_tier": "P1"})]}
tree["children"][0]["labels"] = {"Rating": "B", "Pricing_tier": "P3"}
print("validation:", [i for i in st.validate(tree, df.columns, S) if i["level"] == "error"] or "ok")
res, masks, _ = st.evaluate(df, tree, S)
print(res.groupby("tree_leaf")[["label_Rating", "label_Pricing_tier", "out_apr"]].first().to_string())
ok_inherit = res.loc[res.tree_leaf == "n6", "label_Rating"].eq("BBB").all() and res.loc[res.tree_leaf == "n5", "label_Rating"].eq("A").all() \
    and res.loc[res.tree_leaf == "n5", "label_Pricing_tier"].eq("P2").all()
print("inheritance (n6 inherits BBB, n5 overrides A, keeps P2):", ok_inherit)
con = sqlite3.connect(":memory:"); df.to_sql("input_table", con, index=False)
for nm, f in (("GREATEST", max), ("LEAST", min)): con.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
q = pd.read_sql(st.to_sql(tree, {"name": "T", "score_col": S}), con)
ns = {}; exec(st.to_python(tree, {"name": "T", "score_col": S}), ns)
py = [ns["decide"]({k: (None if pd.isna(v) else v) for k, v in r.items()}) for r in df.to_dict("records")]
for c in ["label_Rating", "label_Pricing_tier", "out_apr", "tree_decision"]:
    a = res[c].astype(object).where(res[c].notna(), None).tolist()
    sq = q[c].astype(object).where(q[c].notna(), None).tolist()
    pv = [d["labels"].get(c[6:]) if c.startswith("label_") else d["outputs"].get("apr") if c == "out_apr" else d["decision"] for d in py]
    eq = lambda u, v: (u is None and v is None) or (u is not None and v is not None and (u == v or (isinstance(u, float) and abs(u - float(v)) < 1e-12)))
    print(f"  {c:20} SQL {np.mean([eq(u, v) for u, v in zip(a, sq)]):.4f}  Python {np.mean([eq(u, v) for u, v in zip(a, pv)]):.4f}")
studio, issues = st.to_studio(tree, {"score_col": S})
print("send to studio – leaf labels:", [l.get("labels") for l in se.leaves(studio)])
sres, _ = se.evaluate(df, studio, S)
sm = lambda col: (sres[col].astype(object).where(sres[col].notna(), None).values == res[col].astype(object).where(res[col].notna(), None).values).mean()
print("Studio evaluate label columns = Tree Builder:", sm("label_Rating"), sm("label_Pricing_tier"))
q2 = pd.read_sql(se.to_sql(studio, {"name": "T", "score_col": S}), con)
print("Studio SQL label agreement:", (q2["label_Rating"].values == sres["label_Rating"].values).mean())
ns2 = {}; exec(se.to_python(studio, {"name": "T", "score_col": S}), ns2)
print("Studio Python label agreement:", np.mean([ns2["decide"]({k: (None if pd.isna(v) else v) for k, v in r.items()})["labels"].get("Rating") == lab
                                                  for r, lab in zip(df.to_dict("records"), sres["label_Rating"])]))
for fmt in ("interconnect", "powercurve"):
    back = se.tree_from_any(json.loads(se.export(fmt, studio, {"name": "T", "score_col": S})))
    print(f"{fmt} round trip keeps labels:", [l.get("labels") for l in se.leaves(back)] == [l.get("labels") for l in se.leaves(studio)])
for fmt in ex.FORMATS:
    txt = se.export(fmt, studio, {"name": "T", "score_col": S})
    if fmt == "dmn":
        ET.fromstring(txt)
    print(f"{fmt:9} carries labels:", "AAA" in txt and ("label_Rating" in txt or "LabelRating" in txt or "labels" in txt))
