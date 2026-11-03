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
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1680, "height": 1000})
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb .rrow", timeout=30000); time.sleep(1)
    pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6); pg.click(".tb .rrow[data-v='vantage_score']")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.6); pg.click(".tb #btnApply"); time.sleep(2)
    leaf = root(pg)["children"][1]["id"]; pg.click(".tb #btnFit"); time.sleep(0.8); node(pg, leaf); time.sleep(0.4)
    pg.click(".tb .tabs button[data-tab='logic']"); time.sleep(0.3)
    check(pg.eval_on_selector_all(".tb #outList .calc-row", "r=>r.length") == 0, "leaf starts without outputs")
    pg.select_option(".tb #lfMode", "output"); time.sleep(0.8)
    rows = pg.eval_on_selector_all(".tb #outList .calc-row", "r=>r.map(x=>[x.querySelector('.cn').value, x.querySelector('.ce').value])")
    check(rows and rows[0][0] == "credit_decision", f"choosing 'from an output…' adds a ready-made output: {rows}")
    check(pg.input_value(".tb #lfDecOut") == "credit_decision", "and selects it in the picker")
    print("   hint:", " ".join(pg.inner_text(".tb #lfDecOutWrap").split()), "| score label:", pg.inner_text(".tb #lfScoreLbl"))
    pg.screenshot(path=OUT + "/tb_13_decision_output.png")
    pg.click(".tb #btnApplyLogic"); time.sleep(2.5)
    t = root(pg); L = [c for c in t["children"] if c["id"] == leaf][0]["leaf"]
    check(L["mode"] == "output" and L["decision_output"] == "credit_decision" and L["outputs"][0]["name"] == "credit_decision", "Apply logic saves mode, output and decision link")
    card = pg.evaluate("id=>{var x='';document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)x=g.textContent});return x}", leaf)
    check("= credit_decision" in card, "card shows '= credit_decision'")
    pg.fill(".tb #outName", "decout_test"); pg.click(".tb #btnWrite"); time.sleep(2)
    out = dataiku.TABLES["decout_test"]; l = out[out.tree_leaf == leaf]
    check((l.tree_decision == l.out_credit_decision).all() and set(l.tree_decision) <= {"APPROVE", "REFER", "DECLINE"},
          f"leaf decisions follow the output: {l.tree_decision.value_counts().to_dict()}")
    # switching away and back does not add a second output
    pg.select_option(".tb #lfMode", "cutoff"); pg.select_option(".tb #lfMode", "output"); time.sleep(0.5)
    check(pg.eval_on_selector_all(".tb #outList .calc-row", "r=>r.length") == 1, "no duplicate output when switching modes")
    b.close()
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
