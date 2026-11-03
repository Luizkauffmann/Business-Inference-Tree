import sys, threading, time, pandas as pd
sys.path[:0] = [sys.argv[2], sys.argv[1]]; OUT = sys.argv[3]
import flask, dataiku
dataiku.TABLES["applications_demo_enriched"] = pd.read_csv(sys.argv[4])
from werkzeug.serving import make_server
app = flask.Flask("s")
from strategy_webapp import register; register(app)
SHELL = '''fetch(dataiku.getWebAppBackendUrl('/ui')).then(r=>r.json()).then(u=>{var s=document.createElement('style');s.textContent=u.css;document.head.appendChild(s);document.getElementById('root').innerHTML=u.html;new Function(u.js)();});'''
@app.route("/")
def index(): return '<!doctype html><html><head><meta charset="utf-8"><style>body{margin:0}</style><script>window.dataiku={getWebAppBackendUrl:function(p){return p;}};</script></head><body><div id="root"></div><script>' + SHELL + '</script></body></html>'
srv = make_server("127.0.0.1", 8774, app, threaded=True); threading.Thread(target=srv.serve_forever, daemon=True).start()
from playwright.sync_api import sync_playwright
errs = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True); pg = b.new_page(viewport={"width": 1600, "height": 960})
    pg.on("pageerror", lambda x: errs.append(str(x))); pg.on("dialog", lambda d: d.accept())
    pg.goto("http://127.0.0.1:8774/"); pg.wait_for_function("()=>document.querySelector('.ss #scoreCol option')", timeout=20000); time.sleep(1)
    pg.select_option(".ss #scoreCol", "policy_score_existing"); pg.select_option(".ss #target", "bad_12m_flag_demo")
    pg.fill(".ss #margin", "350"); pg.fill(".ss #loss", "4200"); pg.fill(".ss #baseline", "800")
    pg.click(".ss #btnLoad"); pg.wait_for_selector(".ss .tn", timeout=30000); time.sleep(2)
    pg.click(".ss .tn >> nth=2"); time.sleep(1)
    pg.screenshot(path=OUT + "/ss_1_tree.png")
    pg.click(".ss .tabs button[data-tab='backtest']"); time.sleep(1.5); pg.screenshot(path=OUT + "/ss_2_backtest.png")
    pg.click(".ss .tabs button[data-tab='export']"); time.sleep(1.5)
    pg.select_option(".ss #expFormat", "powercurve"); time.sleep(1.5); pg.screenshot(path=OUT + "/ss_3_export_powercurve.png")
    pg.select_option(".ss #expFormat", "dmn"); time.sleep(1.5); pg.screenshot(path=OUT + "/ss_4_export_dmn.png")
    print("kpis:", pg.inner_text(".ss #kpis").replace("\n", " "))
    b.close()
print("errors:", errs); srv.shutdown()
