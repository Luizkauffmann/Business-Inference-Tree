import sys, time, numpy as np, pandas as pd
sys.path[:0] = [sys.argv[2], sys.argv[1]]
import flask, dataiku
df = pd.read_csv(sys.argv[3])
wide = pd.DataFrame(np.random.RandomState(0).randn(len(df), 1500).round(3), columns=[f"attr_{i:04d}" for i in range(1500)])
wide["signal_attr"] = df["vantage_score"]; wide["bad_12m_flag_demo"] = df["bad_12m_flag_demo"]; wide["policy_score_existing"] = df["policy_score_existing"]
dataiku.TABLES["wide"] = wide
app = flask.Flask("t"); from tree_builder_webapp import register; register(app); c = app.test_client()
t0 = time.time(); r = c.post("/api/load", json={"dataset": "wide", "target": "bad_12m_flag_demo", "score_col": "policy_score_existing", "margin": 350, "loss": 4200})
print("load", r.status_code, round(time.time() - t0, 2), "s", len(r.data) // 1024, "KB", r.get_json().get("error"))
tree = r.get_json()["starter"]
for ep in ["/api/evaluate", "/api/relevance", "/api/guidance"]:
    t0 = time.time(); r = c.post(ep, json={"tree": tree, "node_id": "n1"}); print(ep, r.status_code, round(time.time() - t0, 2), "s", len(r.data) // 1024, "KB", (r.get_json() or {}).get("error"))
