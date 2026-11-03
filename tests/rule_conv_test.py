"""Send to Studio converts rule splits: the Strategy Studio engine must route every row like the Tree Builder."""
import sys, numpy as np, pandas as pd
sys.path.insert(0, sys.argv[1])
import strategy_tree as st, strategy_engine as se
df = pd.read_csv(sys.argv[2])
SCORE = "policy_score_existing"
rng = np.random.default_rng(7)
for c in ["vantage_score", "debt_to_income_pct", "ltv_pct"]:          # make sure missing values are exercised
    df.loc[rng.random(len(df)) < 0.05, c] = np.nan


def rule_tree(rules, extra=None):
    t = st.starter_tree(SCORE, 800)
    t = st.apply_split(t, "n1", {"kind": "rule", "rules": rules})
    t["label_defs"] = [{"name": "Segment", "values": [f"S{i}" for i in range(len(rules) + 1)]}]
    for i, k in enumerate(t["children"]):
        k["labels"] = {"Segment": f"S{i}"}
        k["leaf"].update({"mode": ["decline", "refer", "approve", "cutoff"][i % 4], "approve_at": 760 + 5 * i,
                          "reason_codes": [f"R{i}"]})
    if extra:
        extra(t)
    return t


def check(name, t, expect_issues=0):
    res, _, _ = st.evaluate(df, t)
    stree, issues = st.to_studio(t, {"score_col": SCORE})
    assert not se.validate(stree, list(df.columns)), se.validate(stree, list(df.columns))
    out, _ = se.evaluate(df, stree, SCORE)
    dec = np.mean(out["strategy_decision"].values == res["tree_decision"].values)
    lab = np.mean(out["label_Segment"].fillna("").values == res["label_Segment"].fillna("").values)
    rc = np.mean(out["reason_codes"].fillna("").values == res["reason_codes"].fillna("").values)
    print(f"{name:34s} decision {dec:.4f} label {lab:.4f} reasons {rc:.4f} issues {len(issues)}")
    assert len(issues) == expect_issues, issues
    if not expect_issues:
        assert dec == 1.0 and lab == 1.0 and rc == 1.0, name
    return issues


check("or / and", rule_tree(["delinquencies_24m_count > 0 or collections_count > 0", "thin_file_flag == 1",
                             "vantage_score >= 760 and debt_to_income_pct < 36"]))
check("not / in / not in", rule_tree(["not (vantage_score >= 650)", "state in ['CA', 'NY', 'TX']",
                                      "state not in ['FL'] and ltv_pct <= 90"]))
check("chained / between / reversed", rule_tree(["600 <= vantage_score < 700", "between(debt_to_income_pct, 20, 40)",
                                                 "45 < ltv_pct"]))
check("isnull / notnull / col()", rule_tree(["isnull(vantage_score) or isnull(ltv_pct)", "notnull(debt_to_income_pct) and col('ltv_pct') > 95",
                                             "not (isnull(debt_to_income_pct) or vantage_score < 700)"]))
check("negative / != / True", rule_tree(["debt_to_income_pct != 30 and vantage_score > -1", "True"]))
iss = check("arithmetic (not convertible)", rule_tree(["vantage_score - 10 * debt_to_income_pct > 300", "thin_file_flag == 1"]), 1)
assert "Otherwise" in iss[0] and "column" in iss[0], iss


def add_calc(t):
    t["calcs"] = [{"name": "dti_x", "expr": "debt_to_income_pct * 2"}]


iss = check("calculation (not convertible)", rule_tree(["dti_x > 70", "thin_file_flag == 1"], add_calc), 2)
assert any("dti_x" in i and "dataset column" in i for i in iss), iss
print("rule conversion OK")
