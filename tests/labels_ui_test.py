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
import strategy_tree as st
errs = []
def check(c, m): print(("PASS " if c else "FAIL ") + m)
def node(pg, nid): pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", nid)
def root(pg): return pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t}")
def card(pg, nid): return pg.evaluate("id=>{var x='';document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)x=Array.from(g.querySelectorAll('g.lblb')).map(b=>b.textContent+(b.querySelector('rect').getAttribute('fill')==='#fff'?'(inh)':'(own)')).join(' ')});return x}", nid)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1680, "height": 1000})
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb .rrow", timeout=30000); time.sleep(1)
    pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6); pg.click(".tb .rrow[data-v='vantage_score']")
    pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.6); pg.click(".tb #btnApply"); time.sleep(2)
    t = root(pg); kids = [c["id"] for c in t["children"]]; pg.click(".tb #btnFit"); time.sleep(0.8)
    node(pg, kids[1]); time.sleep(0.4); pg.click(".tb .tabs button[data-tab='logic']"); time.sleep(0.3)
    check(pg.eval_on_selector_all(".tb #labelRows select", "s=>s.length") == 1 and "Rating" in pg.inner_text(".tb #labelBox"), "Labels box with the default Rating type")
    pg.select_option(".tb #labelRows select[data-label='Rating']", "BBB"); time.sleep(2)
    check("BBB(own)" in card(pg, kids[1]), "card shows its own label badge: " + card(pg, kids[1]))
    # add a second label type through the dialog
    pg.click(".tb #btnLabelDefs"); time.sleep(0.3); pg.click(".tb #btnLdAdd")
    r = pg.query_selector_all(".tb #ldList .ld-row")[-1]; r.query_selector(".ldn").fill("Pricing_tier"); r.query_selector(".ldv").fill("P1, P2, P3")
    pg.click(".tb #btnLdApply"); time.sleep(2)
    check(pg.eval_on_selector_all(".tb #labelRows select", "s=>s.length") == 2, "second label type Pricing_tier added")
    # label the root: inherited by every node below
    node(pg, t["id"]); time.sleep(0.4); pg.select_option(".tb #labelRows select[data-label='Pricing_tier']", "P2"); time.sleep(2)
    check(all("P2(inh)" in card(pg, k) for k in kids) and "P2(own)" in card(pg, t["id"]), "root label inherited by all children (outlined badges)")
    node(pg, kids[3]); time.sleep(0.4)
    opt = pg.eval_on_selector(".tb #labelRows select[data-label='Pricing_tier'] option", "o=>o.textContent")
    check("inherited: P2" in opt, "child shows what it inherits: " + opt)
    pg.select_option(".tb #labelRows select[data-label='Rating']", "AAA"); pg.select_option(".tb #labelRows select[data-label='Pricing_tier']", "P1"); time.sleep(2)
    check("AAA(own)" in card(pg, kids[3]) and "P1(own)" in card(pg, kids[3]) and "inh" not in card(pg, kids[3]), "override on a child: both labels set on the node")
    pg.screenshot(path=OUT + "/tb_14_labels.png")
    # outputs: decisions dataset, SQL export, Send to Studio
    pg.fill(".tb #outName", "labels_out"); pg.click(".tb #btnWrite"); time.sleep(2)
    out = dataiku.TABLES["labels_out"]; g = out.groupby("tree_leaf")[["label_Rating", "label_Pricing_tier"]].first()
    print("   dataset labels:", g.to_dict("index"))
    check(g.loc[kids[1]].tolist() == ["BBB", "P2"] and g.loc[kids[3]].tolist() == ["AAA", "P1"] and pd.isna(g.loc[kids[0], "label_Rating"]) and g.loc[kids[0], "label_Pricing_tier"] == "P2",
          "decisions dataset has label_Rating / label_Pricing_tier with inheritance")
    pg.select_option(".tb #expFmt", "sql")
    with pg.expect_download() as dl:
        pg.click(".tb #btnExport")
    sql = open(dl.value.path()).read(); check('"label_Rating"' in sql and "'AAA'" in sql, "SQL export has the label columns")
    pg.fill(".tb #stName", "LABEL_TEST"); pg.click(".tb #btnStudio"); time.sleep(3)
    print("   studio toast:", pg.inner_text(".tb #toast")[:160], "| files:", [k for k in dataiku.FILES if "strategies" in k[1]])
    stu = json.loads(dataiku.FILES[("decision_strategies", "/strategies/LABEL_TEST.json")])
    def sleaves(n): return [n] if n.get("type") == "leaf" else sleaves(n["yes"]) + sleaves(n["no"])
    labs = [l.get("labels") for l in sleaves(stu["tree"])]; print("   studio leaf labels:", labs)
    check({"Rating": "AAA", "Pricing_tier": "P1"} in labs and {"Rating": "BBB", "Pricing_tier": "P2"} in labs, "Send to Studio carries the labels")
    # renaming a label type renames it on every node
    pg.click(".tb #btnLabelDefs"); time.sleep(0.3); pg.query_selector_all(".tb #ldList .ld-row")[0].query_selector(".ldn").fill("Risk_rating")
    pg.click(".tb #btnLdApply"); time.sleep(2)
    t2 = root(pg); k3 = [c for c in t2["children"] if c["id"] == kids[3]][0]
    check(k3["labels"].get("Risk_rating") == "AAA" and "Rating" not in k3["labels"], "renaming Rating → Risk_rating keeps the values")
    b.close()
import strategy_tree as st  # noqa (used above via closure)
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
