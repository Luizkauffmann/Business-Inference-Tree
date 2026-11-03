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
    pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=30000); pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1)
    cuts = lambda: [float(x) for x in pg.input_value(".tb #sbCuts").split(",") if x.strip()]
    nbins = lambda: pg.eval_on_selector_all(".tb #sbPreview tr.bin", "r=>r.length")
    def settle(): time.sleep(0.25); pg.wait_for_load_state("networkidle"); time.sleep(0.4)
    # numeric: pick vantage_score, optimal split
    pg.fill(".tb #relSearch", "vantage_score"); time.sleep(0.6)
    pg.click(".tb .rrow[data-v='vantage_score']"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); settle()
    c0 = cuts(); print("   optbinning cuts:", c0)
    print("   header:", pg.inner_text(".tb #sbPreview tr >> nth=0").replace("\n", " | "))
    check(nbins() == len(c0) + 1, f"bin table: {nbins()} bins for {len(c0)} cuts, WOE/IV/trend columns present")
    check("Missing" in pg.inner_text(".tb #sbPreview"), "missing row shown with its routing")
    # select groups 1 and 2 -> merge
    pg.click(".tb #sbPreview tr.bin >> nth=0"); pg.click(".tb #sbPreview tr.bin >> nth=1", modifiers=["Meta"]); time.sleep(0.2)
    bar = pg.inner_text(".tb #sbPreview .selbar"); print("   selbar:", bar.replace("\n", " "))
    check("2 bins selected (Groups 1, 2)" in bar, "multi-select bar like Binning v2")
    pg.click(".tb #sbPreview .bMerge"); settle()
    c1 = cuts(); check(c1 == c0[1:], f"merge removed the cut between groups 1 and 2 -> {c1}")
    check("manual" in pg.inner_text(".tb #sbSource"), "source marked as manual override")
    # non-adjacent merge rejected
    if nbins() >= 3:
        pg.click(".tb #sbPreview tr.bin >> nth=0"); pg.click(".tb #sbPreview tr.bin >> nth=2", modifiers=["Meta"]); pg.click(".tb #sbPreview .bMerge"); time.sleep(0.4)
        check(cuts() == c1 and "adjacent" in pg.inner_text(".tb #toast"), "non-adjacent numeric merge refused")
        pg.click(".tb #sbPreview .bClear")
    # split bin 1 with the default (median)
    pg.click(".tb #sbPreview tr.bin >> nth=0 >> button[data-act='split']"); pg.wait_for_selector(".tb #editModal:not([hidden])")
    print("   split dialog:", pg.inner_text(".tb #editBody").replace("\n", " "))
    pg.click(".tb #btnEditApply"); settle()
    c2 = cuts(); check(len(c2) == len(c1) + 1 and c2[0] < c1[0], f"split bin inserted a cut -> {c2}")
    # edit boundary: out of range refused, in range applied
    pg.click(".tb #sbPreview tr.bin >> nth=0 >> button[data-act='edge']"); pg.wait_for_selector(".tb #editModal:not([hidden])")
    print("   edit dialog:", pg.inner_text(".tb #editBody").replace("\n", " "))
    pg.fill(".tb #edVal", str(c2[1] + 1)); pg.click(".tb #btnEditApply"); time.sleep(0.3)
    check(not pg.is_hidden(".tb #editModal") and "valid range" in pg.inner_text(".tb #toast"), "boundary outside the valid range refused")
    newv = round((c2[0] + c2[1]) / 2, 2); pg.fill(".tb #edVal", str(newv)); pg.click(".tb #btnEditApply"); settle()
    check(abs(cuts()[0] - newv) < 1e-6, f"boundary edited to {newv}")
    # missing -> own group, then undo
    before = pg.input_value(".tb #sbMissBr"); target = "0" if before == "sep" else "sep"
    pg.select_option(".tb #sbPreview .missTo", target); settle()
    check(pg.input_value(".tb #sbMissBr") == target, f"missing values rerouted ({before} -> {target})")
    pg.click(".tb #sbPreview .bUndo"); settle()
    check(pg.input_value(".tb #sbMissBr") == before, "undo last bin edit")
    # full table modal
    pg.click(".tb #sbPreview .bFull"); time.sleep(0.4)
    hdr = pg.inner_text(".tb #binModalBody tr >> nth=0"); check("Non-events" in hdr and "IV contrib" in hdr, "full table: " + hdr.replace("\n", " | "))
    pg.screenshot(path=OUT + "/tb_5_bins_full.png"); pg.click(".tb #btnBinClose")
    final = cuts(); pg.click(".tb #btnApply"); time.sleep(2.5)
    tree = pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t}")
    check(tree["split"]["cuts"] == final and tree["split"]["source"] == "manual", f"edited split applied to the tree ({len(tree['children'])} branches)")
    # grow a sub-tree under child 1, then edit the root split again: same branch count keeps sub-trees
    kid = tree["children"][0]["id"]
    pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", kid)
    pg.wait_for_selector(".tb .rrow", timeout=20000); time.sleep(1)
    pg.fill(".tb #relSearch", "debt_to_income_pct"); time.sleep(0.6)
    pg.click(".tb .rrow[data-v='debt_to_income_pct']"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=60000); settle()
    pg.click(".tb #btnApply"); time.sleep(2.5)
    root = tree["id"]
    pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", root); time.sleep(1)
    check(pg.is_visible(".tb #btnEditSplit"), "'Edit this split (bins)' shown on a split node")
    pg.click(".tb #btnEditSplit"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=30000); settle()
    check(cuts() == final, "existing split loaded into the bin editor")
    pg.screenshot(path=OUT + "/tb_6_edit_existing.png")
    pg.click(".tb #sbPreview tr.bin >> nth=1 >> button[data-act='edge']"); pg.wait_for_selector(".tb #editModal:not([hidden])")
    v2 = round((final[1] + final[2]) / 2, 2) if len(final) > 2 else round(final[1] + 0.5, 2)
    pg.fill(".tb #edVal", str(v2)); pg.click(".tb #btnEditApply"); settle(); pg.click(".tb #btnApply"); time.sleep(2.5)
    t2 = pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t}")
    check(abs(t2["split"]["cuts"][1] - v2) < 1e-6 and t2["children"][0].get("split") is not None, "boundary override on an existing split keeps the sub-tree below")
    # categorical: state -> merge two groups, then move categories to a new group
    pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", t2["children"][1]["id"]); time.sleep(1)
    pg.fill(".tb #relSearch", "state"); time.sleep(0.6)
    pg.click(".tb .rrow[data-v='state']"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=60000); settle()
    groups = lambda: [l for l in pg.input_value(".tb #sbGroups").split("\n") if l.strip()]
    g0 = groups(); print("   state groups:", len(g0), "bins", nbins())
    if len(g0) >= 2:
        pg.click(".tb #sbPreview tr.bin >> nth=0"); pg.click(".tb #sbPreview tr.bin >> nth=1", modifiers=["Meta"]); pg.click(".tb #sbPreview .bMerge"); settle()
        check(len(groups()) == len(g0) - 1, "categorical groups merged")
    pg.click(".tb #sbPreview tr.bin >> nth=0 >> button[data-act='split']"); pg.wait_for_selector(".tb #editModal:not([hidden])")
    pg.click(".tb #editBody .pick input >> nth=0"); pg.click(".tb #btnEditApply"); settle()
    gg = groups(); check(len(gg[1].split(",")) == 1, f"category moved to a new group: {gg[:2]}")
    # rules: two rules -> merge -> one OR rule
    pg.click(".tb #btnManual"); pg.fill(".tb #sbRules", "debt_to_income_pct > 45\nvantage_score < 600"); pg.click(".tb #btnPreview"); settle()
    pg.click(".tb #sbPreview tr.bin >> nth=0"); pg.click(".tb #sbPreview tr.bin >> nth=1", modifiers=["Meta"]); pg.click(".tb #sbPreview .bMerge"); settle()
    check(pg.input_value(".tb #sbRules").strip() == "(debt_to_income_pct > 45) or (vantage_score < 600)", "rules merged: " + pg.input_value(".tb #sbRules"))
    pg.screenshot(path=OUT + "/tb_7_rules.png")
    b.close()
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:3])))
srv.shutdown()
