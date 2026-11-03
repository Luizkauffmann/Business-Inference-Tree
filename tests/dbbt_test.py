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
dataiku.SQL_TABLES.add("applications_demo_enriched")
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1680, "height": 1000})
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x))); pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.goto("http://127.0.0.1:8773/"); pg.wait_for_function("()=>document.querySelector('.tb #target') && document.querySelector('.tb #target').value", timeout=20000); time.sleep(0.5)
    # 1) default: all rows in memory (unchanged behaviour)
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=30000); time.sleep(1.5)
    info = pg.inner_text(".tb #popInfo"); check(info.startswith("12,000 rows") and "all rows in memory" in info, "blank = all rows in memory: " + info)
    pg.click(".tb #btnDbBt"); pg.wait_for_selector(".tb #dbModal:not([hidden])", timeout=60000); time.sleep(0.3)
    full = pg.inner_text(".tb #dbBody"); print("   full vs db:", " ".join(full.split()[:40]))
    rows = [l for l in full.split("\n") if l.startswith("Approval") or l.startswith("Rows") or l.startswith("Event rate approved")]
    check(all(("+0.00 pts" in l or "\t0\t" in l or l.endswith("0")) for l in rows) or all(l.split("\t")[1] == l.split("\t")[2] for l in rows),
          "all rows in memory: in-database KPIs equal in-memory KPIs " + str([l.split("\t")[1:3] for l in rows]))
    pg.click(".tb #btnDbClose")
    # 2) sample 3,000 rows, split, then backtest on the full table
    pg.fill(".tb #sampleRows", "3000"); pg.click(".tb #btnLoad"); time.sleep(2.5)
    info = pg.inner_text(".tb #popInfo"); check(info.startswith("3,000 rows") and "random sample" in info, "sample loaded: " + info)
    pg.wait_for_selector(".tb .rrow", timeout=20000); pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6)
    pg.click(".tb .rrow[data-v='vantage_score']"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.6)
    pg.click(".tb #btnApply"); time.sleep(2)
    pg.click(".tb #btnFreeze"); time.sleep(2)
    pg.click(".tb #btnDbBt"); pg.wait_for_selector(".tb #dbModal:not([hidden])", timeout=60000); time.sleep(0.3)
    body = pg.inner_text(".tb #dbBody"); print("   sample vs db:", " ".join(body.split()[:60]))
    check("random sample, 3,000 rows" in body and "12,000" in body, "comparison: sample in memory vs full table in database")
    check("Avg predicted event rate (frozen)" in body and "By leaf" in body and "WITH scored AS" in pg.inner_html(".tb #dbBody"), "frozen rates, per-leaf table and the SQL shown")
    # cross-check the database numbers with an in-memory evaluation of the full table
    tree = pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t}")
    res, masks, _ = st.evaluate_full(df, tree, "policy_score_existing")
    appr = (res["tree_decision"] == "APPROVE").mean()
    check(f"{100 * appr:.1f}%" in body, f"database approval = full-table approval computed in memory ({100 * appr:.1f}%)")
    pg.screenshot(path=OUT + "/tb_11_db_backtest.png"); pg.click(".tb #btnDbClose")
    # 3) non-SQL dataset: clear message, nothing breaks
    dataiku.SQL_TABLES.discard("applications_demo_enriched")
    pg.click(".tb #btnDbBt"); time.sleep(1.5)
    check("SQL database" in pg.inner_text(".tb #toast"), "non-SQL dataset: " + pg.inner_text(".tb #toast")[:90])
    b.close()
real = [e for e in errs if "favicon" not in e and "400" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
