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
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1680, "height": 1000})
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    check(pg.input_value(".tb #target") == "bad_12m_flag_demo" and pg.input_value(".tb #scoreCol") == "policy_score_existing", "defaults detected")
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=30000); time.sleep(1.5)
    print("   kpis:", pg.inner_text(".tb #kpis").replace("\n", " "))
    # 1. relevance scores at the root + guidance
    pg.wait_for_selector(".tb .rrow", timeout=20000)
    rows = pg.eval_on_selector_all(".tb .rrow", "rs=>rs.slice(0,6).map(r=>r.innerText.replace(/\\n/g,' | '))"); print("   relevance top:", rows)
    check(len(rows) > 0 and "100%" in rows[0], "relevance % shown, best attribute = 100%")
    pg.wait_for_selector(".tb #guide .step", timeout=20000); print("   guidance:", pg.inner_text(".tb #guide")[:200])
    # 2. click top attribute -> optbinning multi-way split -> apply (multiple splits on one variable right after root)
    pg.click(".tb .rrow >> nth=0"); pg.wait_for_selector(".tb #sbPreview table", timeout=90000); time.sleep(0.3)
    print("   suggested:", pg.input_value(".tb #sbVar"), "cuts", pg.input_value(".tb #sbCuts"), "|", pg.inner_text(".tb #sbSource"))
    pg.click(".tb #btnApply"); time.sleep(2.5)
    n_nodes = pg.eval_on_selector_all(".tb g.node", "n=>n.length"); check(n_nodes >= 4, f"multi-way split applied at root ({n_nodes} nodes drawn)")
    pg.screenshot(path=OUT + "/tb_1_root_split.png")
    # 3. second layer: split two different children (multiple splits per layer); one via saved Binning v2 config
    kids = pg.evaluate("()=>{var r=[];document.querySelectorAll('.tb g.node').forEach(g=>r.push(g.__data__.data.id));return r}")
    print("   nodes:", kids)
    pg.select_option(".tb #cfgSel", "0")
    def click_node(nid):
        pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", nid)
    click_node(kids[1]); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1.5)
    pg.fill(".tb #relSearch", "debt_to"); time.sleep(0.6)
    check(pg.eval_on_selector_all(".tb .rrow", "r=>r.length") >= 1 and "debt_to_income" in pg.inner_text(".tb #relRows"), "debounced search filters attributes")
    pg.click(".tb .rrow >> nth=0"); pg.wait_for_selector(".tb #sbPreview table", timeout=30000)
    check(pg.is_visible(".tb #btnFromCfg"), "saved Binning v2 configuration offered for this variable")
    pg.click(".tb #btnFromCfg"); time.sleep(1.2); print("   from config:", pg.input_value(".tb #sbCuts"), "|", pg.inner_text(".tb #sbSource"))
    check(pg.input_value(".tb #sbCuts").replace(" ", "") == "20,35,45", "binning config cut-offs used as split")
    pg.click(".tb #btnApply"); time.sleep(2.5)
    click_node(kids[2]); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1.2)
    # manual business-rule split with a node calculation
    pg.click(".tb .tabs button[data-tab=logic]"); pg.click(".tb #btnAddCalc")
    pg.fill(".tb #calcList .calc-row .cn", "adj_score"); pg.fill(".tb #calcList .calc-row .ce", "policy_score_existing - 15 * inquiries_6m_count")
    pg.click(".tb #btnApplyLogic"); time.sleep(2)
    pg.click(".tb .tabs button[data-tab=split]"); time.sleep(1.5); pg.click(".tb #btnManual")
    pg.fill(".tb #sbRules", "debt_to_income_pct > 45 and adj_score < 760\nrevolving_utilization_pct > 85")
    pg.click(".tb #btnPreview"); pg.wait_for_selector(".tb #sbPreview table", timeout=20000)
    print("   rule preview:", pg.inner_text(".tb #sbPreview").replace("\n", " / ")[:250])
    pg.click(".tb #btnApply"); time.sleep(2.5)
    # leaf formula on one rule leaf
    tree = pg.evaluate("()=>{var ids=[];document.querySelectorAll('.tb g.node.leaf').forEach(g=>ids.push(g.__data__.data.id));return ids}")
    click_node(tree[-1]); time.sleep(1); pg.click(".tb .tabs button[data-tab=logic]")
    pg.fill(".tb #lfScore", "adj_score"); pg.fill(".tb #lfApprove", "760"); pg.fill(".tb #lfRefer", "740"); pg.fill(".tb #lfReasons", "HIGH_UTILIZATION")
    pg.fill(".tb #lfLimit", "where(revolving_utilization_pct > 95, 0.5, 0.8)"); pg.click(".tb #btnAddOut")
    pg.fill(".tb #outList .calc-row .cn", "max_payment"); pg.fill(".tb #outList .calc-row .ce", "stated_monthly_income * 0.15")
    pg.click(".tb #btnApplyLogic"); time.sleep(2.5)
    iss = pg.inner_text(".tb #issues") if pg.is_visible(".tb #issues") else ""; check("✖" not in iss, "tree valid after formulas" + (" | " + iss[:200] if iss else ""))
    # 4. metrics: add entropy/gini default + custom metric, persists
    pg.click(".tb #btnMetrics"); pg.check(".tb #metricChecks input[value=split_iv]"); pg.check(".tb #metricChecks input[value=approval_rate]")
    pg.click(".tb #btnAddCustom"); pg.fill(".tb #customList .cn", "avg_dti"); pg.fill(".tb #customList .ce", "debt_to_income_pct")
    pg.click(".tb #btnMetricsSave"); time.sleep(2.5)
    card = pg.evaluate("()=>{var g=[...document.querySelectorAll('.tb g.node')].find(g=>g.__data__.data.id==='n1');return g?g.textContent:''}")
    print("   root card:", card[:300])
    check("Gini impurity" in card and "Entropy" in card and "Dominant class" in card and "avg_dti" in card and "Split IV" in card, "default + custom metrics on node cards")
    check("dti" in (pg.evaluate("()=>localStorage.getItem('tb_display_v1')") or ""), "metric display remembered (localStorage)")
    pg.click(".tb #btnFit"); time.sleep(1); pg.screenshot(path=OUT + "/tb_2_tree.png")
    click_node("n1"); time.sleep(0.5); pg.click(".tb .tabs button[data-tab=metrics]"); time.sleep(0.5)
    print("   n1 metrics:", pg.inner_text(".tb #nodeMetrics").replace("\n", " | ")[:300])
    # 5. save, export python/sql, send to studio, write dataset
    pg.fill(".tb #stName", "TREE_TEST_1"); pg.click(".tb #btnSave"); time.sleep(1)
    check(("decision_strategies", "/trees/TREE_TEST_1.json") in dataiku.FILES, "saved to decision_strategies/trees")
    pg.click(".tb #btnStudio"); time.sleep(2.5); print("   studio toast:", pg.inner_text(".tb #toast"))
    check(("decision_strategies", "/strategies/TREE_TEST_1.json") in dataiku.FILES, "sent to Strategy Studio (after confirming simplifications)")
    pg.fill(".tb #outName", "tree_out_test"); pg.click(".tb #btnWrite"); time.sleep(2)
    check("tree_out_test" in dataiku.TABLES, "decisions dataset written")
    # engine vs saved tree + python export
    import strategy_tree as st
    doc = json.loads(dataiku.FILES[("decision_strategies", "/trees/TREE_TEST_1.json")])
    res, _, _ = st.evaluate(df, doc["tree"], "policy_score_existing")
    ns = {}; exec(st.to_python(doc["tree"], {"score_col": "policy_score_existing"}), ns)
    recs = df.where(df.notna(), None).to_dict("records")
    check(np.mean([ns["decide"](r)["decision"] == d for r, d in zip(recs, res["tree_decision"])]) == 1.0, "Python export reproduces every decision")
    print("   summary:", st.summary(res, df, "bad_12m_flag_demo", 350, 4200))
    # 6. Visual ML import
    pg.select_option(".tb #vmlSel", "0"); pg.click(".tb #btnVml"); time.sleep(4)
    print("   toast:", pg.inner_text(".tb #toast"))
    tree_now = pg.evaluate("()=>{var g=document.querySelector('.tb g.node');return g?g.__data__.data:null}")
    nleaves = pg.evaluate("()=>document.querySelectorAll('.tb g.node').length"); print("   VML nodes drawn:", nleaves)
    pg.click(".tb #btnExpand"); time.sleep(1.5); pg.screenshot(path=OUT + "/tb_3_vml.png")
    pg.fill(".tb #stName", "VML_IMPORT"); pg.click(".tb #btnSave"); time.sleep(1)
    vdoc = json.loads(dataiku.FILES[("decision_strategies", "/trees/VML_IMPORT.json")])
    rv, _, _ = st.evaluate(df, vdoc["tree"])
    check((rv["tree_decision"].values == SK_PRED).mean() == 1.0, "Visual ML tree imported: decisions identical to the model (%.4f)" % (rv["tree_decision"].values == SK_PRED).mean())
    # 7. 1,500 attributes: virtualised list, search, render
    pg.select_option(".tb #ds", "wide_1500_attributes"); time.sleep(1.5)
    pg.select_option(".tb #target", "bad_12m_flag_demo"); pg.select_option(".tb #scoreCol", "policy_score_existing")
    t0 = time.time(); pg.click(".tb #btnLoad"); time.sleep(8); print("   toast/popinfo:", pg.inner_text(".tb #toast") if pg.is_visible(".tb #toast") else "", "|", pg.inner_text(".tb #popInfo")); pg.wait_for_selector(".tb .rrow", timeout=60000)
    pg.wait_for_function("()=>document.querySelector('.tb #relInfo').textContent.indexOf('attributes')>0", timeout=30000)
    print("   wide load+rank: %.1fs" % (time.time() - t0), pg.inner_text(".tb #relInfo")[:80])
    dom_rows = pg.eval_on_selector_all(".tb .rrow", "r=>r.length"); check(dom_rows < 40, f"virtualised list: {dom_rows} DOM rows for 1,500+ attributes")
    check("signal_attr" in pg.inner_text(".tb .rrow >> nth=0"), "relevance finds the informative attribute among 1,500 noise columns")
    pg.evaluate("()=>{var l=document.querySelector('.tb #relList');l.scrollTop=20000;l.dispatchEvent(new Event('scroll'))}"); time.sleep(0.4)
    print("   after scroll:", pg.inner_text(".tb .rrow >> nth=0")[:40])
    t0 = time.time(); pg.fill(".tb #relSearch", "attr_14"); time.sleep(0.5)
    print("   search rows:", pg.eval_on_selector_all(".tb .rrow", "r=>r.length"), "first:", pg.inner_text(".tb .rrow >> nth=0")[:30])
    pg.screenshot(path=OUT + "/tb_4_wide.png")
    b.close()
print("errors:", [e for e in errs if "favicon" not in e and "404" not in e]); srv.shutdown()
