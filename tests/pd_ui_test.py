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
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    pg.evaluate("()=>localStorage.clear()")
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=30000); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1.5)
    check("Avg predicted event rate" in pg.inner_text(".tb #kpis"), "KPI strip: " + pg.inner_text(".tb #kpis").replace("\n", " "))
    check("Live" in pg.inner_text(".tb #pdStatus"), "status live (in-sample): " + pg.inner_text(".tb #pdStatus").replace("\n", " "))
    # build: root split on vantage_score, then debt_to_income_pct under the right child
    pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6); pg.click(".tb .rrow[data-v='vantage_score']")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.8); pg.click(".tb #btnApply"); time.sleep(2.5)
    t = root(pg); kid = t["children"][-1]["id"]; node(pg, kid); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1)
    pg.fill(".tb #relSearch", "debt_to_income_pct"); time.sleep(0.6); pg.click(".tb .rrow[data-v='debt_to_income_pct']")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=60000); time.sleep(0.8); pg.click(".tb #btnApply"); time.sleep(2.5)
    t = root(pg); leaf = t["children"][-1]["children"][-1]["id"]
    pg.click(".tb #btnFit"); time.sleep(1); node(pg, leaf); time.sleep(0.5); pg.click(".tb .tabs button[data-tab='logic']"); time.sleep(0.5)
    box = pg.inner_text(".tb #lfPd"); print("   leaf PD box:", box.replace("\n", " "))
    card = pg.evaluate("id=>{var x='';document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)x=g.textContent});return x}", leaf)
    print("   card:", card[:300]); print("   display:", pg.evaluate("()=>JSON.parse(localStorage.getItem('tb_display_v1')||'null')"))
    check(leaf in pg.inner_text(".tb #nodeSub") and "predicted event rate" in box and "Predicted event rate" in card, "leaf shows predicted event rate in Logic tab and on its card")
    # credibility 0 -> PD equals observed bad rate exactly
    resp = []; pg.on("response", lambda r: resp.append(r) if r.url.endswith("/api/evaluate") else None)
    reqs = []; pg.on("request", lambda r: reqs.append(r.post_data) if r.url.endswith("/api/evaluate") else None)
    t0 = time.time()
    with pg.expect_response(lambda r: r.url.endswith("/api/evaluate"), timeout=60000):
        pg.fill(".tb #pdCred", "0"); pg.dispatch_event(".tb #pdCred", "change")
    print("   evaluate took %.1fs" % (time.time() - t0)); time.sleep(0.5)
    rj = resp[-1].json()["pd"]; print("   resp leaf:", rj["leaves"].get(leaf), rj["settings"])
    print("   toast:", pg.inner_text(".tb #toast"), pg.is_visible(".tb #toast")); print("   evaluate calls after change:", len(reqs), [json.loads(x)["tree"].get("pd_settings") for x in reqs])
    box0 = pg.inner_text(".tb #lfPd"); import re; print("   after cred=0:", pg.input_value(".tb #pdCred"), box0.replace("\n"," "))
    nums = re.findall(r"([\d.]+)%", box0); check(nums[0] == nums[1], f"credibility 0: predicted {nums[0]}% = observed {nums[1]}%")
    pg.fill(".tb #pdCred", "30"); pg.dispatch_event(".tb #pdCred", "change")
    pg.fill(".tb #pdHold", "30"); pg.dispatch_event(".tb #pdHold", "change"); time.sleep(2)
    pg.click(".tb #btnFreeze"); time.sleep(2.5)
    check("Frozen" in pg.inner_text(".tb #pdStatus"), "frozen: " + pg.inner_text(".tb #pdStatus").replace("\n", " "))
    pg.click(".tb #btnCalib"); time.sleep(0.6)
    cal = pg.inner_text(".tb #calBody"); print("   calibration:", cal[:400].replace("\n", " "))
    check("30% holdout" in cal and "AUC" in cal, "calibration measured on the holdout")
    pg.screenshot(path=OUT + "/tb_8_calibration.png"); pg.click(".tb #btnCalClose")
    pg.screenshot(path=OUT + "/tb_9_pd_tree.png")
    # write dataset -> prediction columns like a Visual ML scoring
    pg.fill(".tb #outName", "tree_pd_out"); pg.click(".tb #btnWrite"); time.sleep(2.5)
    out = dataiku.TABLES.get("tree_pd_out")
    cols = [c for c in (out.columns if out is not None else []) if c.startswith(("tree_p", "tree_event"))]
    check(out is not None and {"tree_proba_1", "tree_proba_0", "tree_prediction", "tree_event_rate", "tree_event_rate_raw", "tree_event_n_known"} <= set(cols), f"output dataset columns: {cols}")
    if out is not None:
        g = out.groupby("tree_leaf")["tree_proba_1"].nunique().max(); print("   source:", out["tree_event_rate_source"].iloc[0], "| prediction counts:", out["tree_prediction"].value_counts().to_dict())
        check(g == 1, "one PD per leaf (every row of a leaf gets its leaf PD)")
    # save, edit a cut -> stale
    pg.fill(".tb #stName", "PD_TEST"); pg.click(".tb #btnSave"); time.sleep(1.5)
    saved = json.loads(dataiku.FILES[("decision_strategies", "/trees/PD_TEST.json")].decode() if isinstance(dataiku.FILES[("decision_strategies", "/trees/PD_TEST.json")], bytes) else dataiku.FILES[("decision_strategies", "/trees/PD_TEST.json")])
    check(saved["tree"].get("pd_model", {}).get("status") == "frozen", "frozen PDs saved with the tree")
    node(pg, t["id"]); time.sleep(0.5); pg.click(".tb .tabs button[data-tab='split']"); time.sleep(0.3); pg.click(".tb #btnEditSplit")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=30000); time.sleep(0.8)
    pg.click(".tb #sbPreview tr.bin >> nth=0 >> button[data-act='edge']"); pg.wait_for_selector(".tb #editModal:not([hidden])")
    lo, hi = [float(x) for x in re.findall(r"Valid range: ([\d.]+) – ([\d.]+)", pg.inner_text(".tb #editBody"))[0]]
    pg.fill(".tb #edVal", str(round((lo + hi) / 2, 1))); pg.click(".tb #btnEditApply"); time.sleep(1); pg.click(".tb #btnApply"); time.sleep(2.5)
    check("changed since freezing" in pg.inner_text(".tb #pdStatus"), "editing a cut marks the frozen PDs stale")
    b.close()
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
