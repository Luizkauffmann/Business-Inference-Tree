import io, json, contextlib, pandas as pd
TABLES, FILES, RAW_TREE = {}, {}, {}
SQL_TABLES = set()          # datasets that behave as stored in a SQL database (SQLite here)
_SQL = {}
def _conn():
    import sqlite3
    if "c" not in _SQL:
        c = sqlite3.connect(":memory:", check_same_thread=False)
        for nm, f in (("GREATEST", max), ("LEAST", min)):
            c.create_function(nm, -1, lambda *a, f=f: None if any(x is None for x in a) else f(a))
        _SQL["c"], _SQL["loaded"] = c, set()
    for n in SQL_TABLES - _SQL["loaded"]:
        TABLES[n].to_sql(n, _SQL["c"], index=False); _SQL["loaded"].add(n)
    return _SQL["c"]
class Dataset:
    def __init__(s, n): s.n = n
    def get_dataframe(s, sampling="full", limit=None, **k):
        d = TABLES[s.n]
        return (d.sample(n=min(int(limit), len(d)), random_state=1) if sampling == "random" and limit else d).copy()
    def get_location_info(s):
        return {"locationInfoType": "SQL", "info": {"quotedResolvedTableName": '"%s"' % s.n}} if s.n in SQL_TABLES else {"locationInfoType": "FS", "info": {}}
    def read_schema(s): return [{"name": c} for c in TABLES[s.n].columns]
class Folder:
    def __init__(s, n): s.n = n
    def list_paths_in_partition(s): return [p for (f, p) in FILES if f == s.n]
    @contextlib.contextmanager
    def get_writer(s, p):
        b = io.BytesIO(); yield b; FILES[(s.n, p)] = b.getvalue()
    @contextlib.contextmanager
    def get_download_stream(s, p):
        if (s.n, p) not in FILES: raise IOError("no file")
        yield io.BytesIO(FILES[(s.n, p)])
class _Trees:
    def get_raw(s): return json.loads(json.dumps(RAW_TREE))
class _Details:
    def get_trees(s): return _Trees()
    def get_preprocessing_settings(s): return {}
class _MT:
    def get_trained_models_ids(s): return ["A-P-a1-t1-s1-pp1-m1"]
    def get_trained_model_snippet(s, i): return {"algorithm": "DECISION_TREE_CLASSIFICATION", "userMeta": {"name": "Decision Tree (s3)"}, "auc": 0.87}
    def get_trained_model_details(s, i): return _Details()
class _An:
    def list_ml_tasks(s): return {"mlTasks": [{"mlTaskId": "t1"}]}
    def get_ml_task(s, t): return _MT()
class _DSettings:
    def get_raw(s): return {"customMeta": {"kv": {}}, "params": {}, "managed": True}
    def save(s): pass
class _DS:
    def get_settings(s): return _DSettings()
class _P:
    def list_datasets(s): return [{"name": n} for n in TABLES]
    def get_dataset(s, n): return _DS()
    def list_analyses(s): return [{"analysisId": "a1", "analysisName": "Existing Decisioning"}]
    def get_analysis(s, a): return _An()
    def list_saved_models(s): return []
class _C:
    def get_default_project(s): return _P()
    def get_auth_info_from_browser_headers(s, h): return {"authIdentifier": "tester"}
def api_client(): return _C()
class SQLExecutor2:
    def __init__(s, dataset=None, connection=None): pass
    def query_to_df(s, q): return pd.read_sql(q, _conn())
