import sys, threading, time, json, numpy as np, pandas as pd
sys.path[:0] = [sys.argv[2], sys.argv[1]]; OUT = sys.argv[3]
import flask, dataiku
from sklearn.tree import DecisionTreeClassifier
df = pd.read_csv(sys.argv[4]); dataiku.TABLES["applications_demo_enriched"] = df
wide = pd.DataFrame(np.random.RandomState(0).randn(len(df), 1500).round(3), columns=[f"attr_{i:04d}" for i in range(1500)])
wide["signal_attr"] = df["vantage_score"]; wide["bad_12m_flag_demo"] = df["bad_12m_flag_demo"]; wide["policy_score_existing"] = df["policy_score_existing"]
dataiku.TABLES["wide_1500_attributes"] = wide
feats = ["vantage_score", "debt_to_income_pct", "ltv_pct"]
X = df[feats].fillna(df[feats].mean()); Z = (X - X.mean()) / X.std(ddof=1)
Z = pd.concat([Z, pd.get_dummies(df["state"], prefix="dummy:state", prefix_sep=":").astype(float)], axis=1)
yy = (df["existing_decision"] == "Decline").astype(int)
clf = DecisionTreeClassifier(max_depth=5, random_state=0).fit(Z.values, yy); tr = clf.tree_
dataiku.RAW_TREE.update({"featureNames": list(Z.columns), "classes": ["Approve", "Decline"], "trees": [{
  "leftChild": tr.children_left.tolist(), "rightChild": tr.children_right.tolist(), "feature": tr.feature.tolist(),
  "threshold": tr.threshold.tolist(), "nSamples": tr.n_node_samples.tolist(),
  "probas": (tr.value[:, 0, :] / tr.value[:, 0, :].sum(1, keepdims=True)).tolist()}]})
SK_PRED = np.where(clf.predict_proba(Z.values)[:, 0] >= 0.5, "APPROVE", "DECLINE")
dataiku.FILES[("binning_configs", "/configs/demo_binning.json")] = json.dumps({"type": "scorecard_binning_config", "name": "demo_binning",
  "target": "bad_12m_flag_demo", "saved_at": "2026-10-20T10:00:00Z", "variables": {
  "debt_to_income_pct": {"dtype": "numerical", "cutoffs": [20, 35, 45], "missing": "separate"},
  "state": {"dtype": "categorical", "cat_groups": [["CA", "NY", "WA"], ["TX", "FL"], ["IL", "OH", "PA"]], "missing": "similar"}}}).encode()
from werkzeug.serving import make_server
app = flask.Flask("t")
from tree_builder_webapp import register; register(app)
SHELL = '''fetch(dataiku.getWebAppBackendUrl('/ui')).then(r=>r.json()).then(u=>{var s=document.createElement('style');s.textContent=u.css;document.head.appendChild(s);document.getElementById('root').innerHTML=u.html;new Function(u.js)();});'''
@app.route("/")
def index(): return '<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}</style><script>window.dataiku={getWebAppBackendUrl:function(p){return p;}};</script></head><body><div id="root"></div><script>' + SHELL + '</script></body></html>'
@app.route("/favicon.ico")
def fav(): return ""
srv = make_server("127.0.0.1", 8773, app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
from playwright.sync_api import sync_playwright
errs = []
def check(c, m): print(("PASS " if c else "FAIL ") + m)
def node(pg, nid): pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", nid)
def root(pg): return pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t}")
def add_row(pg, btn, lst, name, expr):
    pg.click(".tb " + btn); rows = pg.query_selector_all(".tb " + lst + " .calc-row"); r = rows[-1]
    r.query_selector(".cn").fill(name); r.query_selector(".ce").fill(expr)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1680, "height": 1000})
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=30000); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1)
    pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6); pg.click(".tb .rrow[data-v='vantage_score']")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.8); pg.click(".tb #btnApply"); time.sleep(2.5)
    t = root(pg); kids = [c["id"] for c in t["children"]]
    # root: default outputs (pricing for all leaves)
    pg.click(".tb #btnFit"); time.sleep(0.8); node(pg, t["id"]); time.sleep(0.5); pg.click(".tb .tabs button[data-tab='logic']"); time.sleep(0.4)
    check(pg.is_visible(".tb #defOutBox"), "default outputs box shown on the root")
    add_row(pg, "#btnAddDefOut", "#defOutList", "apr", "round(0.099 + 1.2 * leaf_event_rate, 4)")
    add_row(pg, "#btnAddDefOut", "#defOutList", "max_amount", "0 if leaf_decision == 'DECLINE' else round(stated_monthly_income * 3, 0)")
    pg.click(".tb #btnApplyLogic"); time.sleep(2)
    # leaf 1: decision from an output
    node(pg, kids[1]); time.sleep(0.5)
    check("apr" in pg.inner_text(".tb #inheritedOuts"), "leaf lists the inherited default outputs: " + pg.inner_text(".tb #inheritedOuts"))
    add_row(pg, "#btnAddOut", "#outList", "credit_decision", "'APPROVE' if leaf_event_rate < 0.05 and leaf_score >= 760 else ('REFER' if leaf_event_rate < 0.08 else 'DECLINE')")
    add_row(pg, "#btnAddOut", "#outList", "risk_grade", "'A' if leaf_event_rate < 0.03 else 'B'")
    pg.select_option(".tb #lfMode", "output"); time.sleep(0.3)
    opts = pg.eval_on_selector_all(".tb #lfDecOut option", "o=>o.map(x=>x.textContent)")
    check(set(["credit_decision", "risk_grade", "apr", "max_amount"]) <= set(opts), f"decision output picker lists leaf + default outputs {opts}")
    pg.select_option(".tb #lfDecOut", "credit_decision"); pg.click(".tb #btnApplyLogic"); time.sleep(2.5)
    # leaf 2: segment only
    node(pg, kids[2]); time.sleep(0.5); pg.select_option(".tb #lfMode", "none"); pg.click(".tb #btnApplyLogic"); time.sleep(2.5)
    t = root(pg)
    check(t["children"][1]["leaf"]["mode"] == "output" and t["children"][1]["leaf"]["decision_output"] == "credit_decision", "leaf saved with decision from output")
    check(len(t.get("default_outputs", [])) == 2, "default outputs saved on the tree")
    kp = pg.inner_text(".tb #kpis").replace("\n", " "); print("   kpis:", kp)
    check("No decision" in kp, "KPI strip shows the no-decision share")
    cards = pg.evaluate("()=>{var r={};document.querySelectorAll('.tb g.node').forEach(g=>{r[g.__data__.data.id]=g.textContent});return r}")
    check("= credit_decision" in cards.get(kids[1], "") and "SEGMENT" in cards.get(kids[2], ""), "card chips: '= credit_decision' and 'SEGMENT'")
    iss = pg.inner_text(".tb #issues") if pg.is_visible(".tb #issues") else ""
    check("error" not in iss.lower() and "✖" not in iss, "no validation errors " + iss[:120])
    pg.screenshot(path=OUT + "/tb_10_outputs.png")
    pg.fill(".tb #outName", "tree_outputs_test"); pg.click(".tb #btnWrite"); time.sleep(2.5)
    out = dataiku.TABLES.get("tree_outputs_test")
    cols = [c for c in out.columns if c.startswith("out_")] if out is not None else []
    check({"out_apr", "out_max_amount", "out_credit_decision", "out_risk_grade"} <= set(cols), f"dataset output columns {cols}")
    if out is not None:
        l1 = out[out.tree_leaf == kids[1]]
        check((l1.tree_decision == l1.out_credit_decision).all(), "leaf decision equals its credit_decision output")
        check(out[out.tree_leaf == kids[2]].tree_decision.isna().all(), "segment-only leaf has no decision")
        print("   apr by leaf:", out.groupby("tree_leaf").out_apr.first().to_dict())
    pg.select_option(".tb #expFmt", "sql")
    with pg.expect_download() as dl:
        pg.click(".tb #btnExport")
    sql = open(dl.value.path()).read()
    check('"out_credit_decision"' in sql and "leaf_event_rate" in sql, "SQL export carries outputs and notes the embedded leaf rates")
    b.close()
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
