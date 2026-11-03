"""End-to-end test of the GitHub Pages demo (docs/demo) in headless Chrome.
Serves docs/ locally; Pyodide itself is loaded from the jsDelivr CDN (network needed).

    python tests/demo_browser_test.py <out_dir>
"""
import http.server, functools, os, sys, threading, time
from playwright.sync_api import sync_playwright
OUT = sys.argv[1] if len(sys.argv) > 1 else "build/out"
os.makedirs(OUT, exist_ok=True)
root = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs")
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8777), functools.partial(http.server.SimpleHTTPRequestHandler, directory=root))
threading.Thread(target=srv.serve_forever, daemon=True).start()
errs = []
def check(c, m): print(("PASS " if c else "FAIL ") + m)
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    ctx = b.new_context(viewport={"width": 1600, "height": 960}, accept_downloads=True)
    pg = ctx.new_page()
    pg.on("pageerror", lambda x: errs.append("PAGEERROR " + str(x)))
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" and "favicon" not in m.text else None)
    pg.on("dialog", lambda d: d.accept())
    t0 = time.time(); pg.goto("http://127.0.0.1:8777/demo/tree.html")
    pg.wait_for_function("()=>/Ready/.test(document.getElementById('bitStatus').textContent)", timeout=240000)
    check(True, f"Tree Builder booted in the browser in {time.time() - t0:.0f}s")
    time.sleep(1.5)
    check(pg.input_value(".tb #target") == "default_next_month" and pg.input_value(".tb #scoreCol") == "behaviour_score",
          f"sample pre-selected: target {pg.input_value('.tb #target')}, score {pg.input_value('.tb #scoreCol')}")
    t0 = time.time(); pg.click(".tb #btnLoad"); pg.wait_for_selector(".tb g.node", timeout=120000); pg.wait_for_selector(".tb .rrow", timeout=120000)
    check(True, f"population loaded ({pg.inner_text('.tb #popInfo')}) in {time.time() - t0:.1f}s")
    top = pg.eval_on_selector_all(".tb .rrow", "r=>r.slice(0,5).map(x=>x.getAttribute('data-v'))"); print("   top attributes:", top)
    pg.click(".tb .rrow >> nth=0"); pg.wait_for_selector(".tb #sbPreview table.bins", timeout=120000); time.sleep(0.5)
    src = pg.inner_text(".tb #sbSource"); print("   split:", pg.input_value(".tb #sbCuts") or pg.input_value(".tb #sbGroups"), "|", src)
    check("dynamic programming" in src, "optimal split via DP binning (no optbinning in the browser)")
    pg.click(".tb #btnApply"); time.sleep(2)
    check(pg.eval_on_selector_all(".tb g.node", "n=>n.length") >= 3, "split applied")
    pg.click(".tb #btnFit"); time.sleep(0.5)
    rid = pg.evaluate("()=>{var t=null;document.querySelectorAll('.tb g.node').forEach(g=>{if(!g.__data__.parent)t=g.__data__.data});return t.id}")
    pg.evaluate("id=>{document.querySelectorAll('.tb g.node').forEach(g=>{if(g.__data__.data.id===id)g.dispatchEvent(new MouseEvent('click',{bubbles:true}))})}", rid)
    pg.click(".tb .tabs button[data-tab='logic']"); time.sleep(0.3)
    pg.click(".tb #btnAddDefOut"); r = pg.query_selector_all(".tb #defOutList .calc-row")[-1]
    r.query_selector(".cn").fill("apr"); r.query_selector(".ce").fill("round(0.12 + 0.5 * leaf_event_rate, 4)")
    pg.click(".tb #btnApplyLogic"); time.sleep(2)
    kp = pg.inner_text(".tb #kpis").replace("\n", " "); print("   kpis:", kp)
    check("Avg predicted event rate" in kp, "KPIs and event-rate prediction work")
    pg.fill(".tb #pdHold", "30"); pg.dispatch_event(".tb #pdHold", "change"); time.sleep(1.5)
    pg.click(".tb #btnFreeze"); time.sleep(2); pg.click(".tb #btnCalib"); time.sleep(0.5)
    cal = pg.inner_text(".tb #calBody"); check("AUC" in cal and "holdout" in cal, "calibration on the holdout: " + " ".join(cal.split()[:26]))
    pg.screenshot(path=OUT + "/demo_1_calibration.png"); pg.click(".tb #btnCalClose")
    pg.screenshot(path=OUT + "/demo_2_tree.png")
    pg.select_option(".tb #expFmt", "sql")
    with pg.expect_download() as dl:
        pg.click(".tb #btnExport")
    sql = open(dl.value.path()).read(); check("out_apr" in sql and "tree_proba_1" in sql, "SQL export downloaded with outputs and frozen rates")
    with pg.expect_download() as dl:
        pg.click(".tb #btnWrite")
    check(dl.value.suggested_filename.endswith(".csv"), "Write decisions dataset downloads " + dl.value.suggested_filename)
    pg.fill(".tb #stName", "DEMO_TREE"); pg.click(".tb #btnSave"); time.sleep(1)
    pg.click(".tb #btnStudio"); time.sleep(2); print("   toast:", pg.inner_text(".tb #toast"))
    # upload a CSV (agnostic data)
    csv = os.path.join(OUT, "tiny.csv")
    open(csv, "w").write("x,grade,y\n" + "\n".join(f"{i % 50},{'AB'[i % 2]},{int(i % 7 == 0)}" for i in range(600)))
    pg.set_input_files("#bitUpload", csv); time.sleep(1.5)
    check(pg.input_value(".tb #ds") == "tiny" and pg.input_value(".tb #target") == "", f"uploaded CSV selected as dataset ({pg.input_value('.tb #ds')})")
    # Strategy Studio sees the strategy sent from the Tree Builder (shared browser storage)
    pg.goto("http://127.0.0.1:8777/demo/studio.html")
    pg.wait_for_function("()=>/Ready/.test(document.getElementById('bitStatus').textContent)", timeout=240000); time.sleep(1)
    pg.select_option(".ss #scoreCol", "behaviour_score"); pg.select_option(".ss #target", "default_next_month")
    pg.click(".ss #btnLoad"); pg.wait_for_selector(".ss .tn", timeout=120000); time.sleep(2)
    lst = pg.inner_text(".ss #stratList"); check("DEMO_TREE" in lst, "Strategy Studio lists the strategy sent from the Tree Builder")
    print("   studio kpis:", pg.inner_text(".ss #kpis").replace("\n", " ")[:160])
    pg.screenshot(path=OUT + "/demo_3_studio.png")
    pg.click(".ss .tabs button[data-tab='export']"); time.sleep(1); pg.select_option(".ss #expFormat", "dmn"); time.sleep(1.5)
    check("<definitions" in pg.inner_text(".ss #expPreview"), "DMN export renders in Studio")
    b.close()
real = [e for e in errs if "favicon" not in e]
check(not real, "no JS errors" + ("" if not real else ": " + str(real[:4])))
srv.shutdown()
