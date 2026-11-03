"""In-browser stand-in for the `dataiku` package, used by the GitHub Pages demo.

The webapp backends run unchanged in Pyodide; this module gives them datasets (pandas
DataFrames in memory), managed folders (kept in the browser's localStorage so a strategy
sent from the Tree Builder appears in Strategy Studio) and an empty Visual ML catalogue.
"""
import contextlib
import io
import json

try:
    import js
except ImportError:          # plain Python (tests)
    js = None

TABLES = {}        # dataset name -> DataFrame
FILES = {}         # (folder, path) -> bytes
_LS_KEY = "bit_demo_folders_v1"


def _persist():
    if js is None:
        return
    try:
        js.localStorage.setItem(_LS_KEY, json.dumps({f"{f}|{p}": b.decode("utf-8") for (f, p), b in FILES.items()}))
    except Exception:
        pass


def _restore():
    if js is None:
        return
    try:
        raw = js.localStorage.getItem(_LS_KEY)
        if raw:
            for k, v in json.loads(raw).items():
                f, p = k.split("|", 1)
                FILES[(f, p)] = v.encode("utf-8")
    except Exception:
        pass


_restore()


class Dataset(object):
    def __init__(self, name):
        self.name = name

    def get_dataframe(self, sampling="full", limit=None, **_):
        if self.name not in TABLES:
            raise ValueError(f"Dataset '{self.name}' is not loaded.")
        d = TABLES[self.name]
        if sampling == "random" and limit:
            d = d.sample(n=min(int(limit), len(d)), random_state=42)
        return d.copy()

    def get_location_info(self):
        return {"locationInfoType": "BROWSER", "info": {}}   # no SQL database in the browser

    def read_schema(self):
        return [{"name": c} for c in TABLES[self.name].columns]

    def write_with_schema(self, df):
        TABLES[self.name] = df.copy()
        if js is not None:          # hand the written dataset to the user as a CSV download
            try:
                js.window.bitDownload(self.name + ".csv", df.to_csv(index=False))
            except Exception:
                pass


class Folder(object):
    def __init__(self, name):
        self.name = name

    def list_paths_in_partition(self):
        return [p for (f, p) in FILES if f == self.name]

    @contextlib.contextmanager
    def get_writer(self, path):
        buf = io.BytesIO()
        yield buf
        FILES[(self.name, path)] = buf.getvalue()
        _persist()

    @contextlib.contextmanager
    def get_download_stream(self, path):
        if (self.name, path) not in FILES:
            raise IOError(f"No file {path} in folder {self.name}.")
        yield io.BytesIO(FILES[(self.name, path)])


class _Settings(object):
    def __init__(self):
        self._raw = {"params": {"connection": "browser"}, "managed": True, "customMeta": {"kv": {}}}

    def get_raw(self):
        return self._raw

    def save(self):
        pass


class _DatasetHandle(object):
    def get_settings(self):
        return _Settings()


class _Creator(object):
    def __init__(self, name):
        self.name = name

    def with_store_into(self, connection):
        return self

    def create(self):
        return _DatasetHandle()


class _Project(object):
    def list_datasets(self):
        return [{"name": n} for n in TABLES]

    def get_dataset(self, name):
        return _DatasetHandle()

    def new_managed_dataset(self, name):
        return _Creator(name)

    def list_analyses(self):
        return []

    def list_saved_models(self):
        return []


class _Client(object):
    def get_default_project(self):
        return _Project()

    def get_auth_info_from_browser_headers(self, headers):
        return {"authIdentifier": "demo-user"}


def api_client():
    return _Client()


def default_project_key():
    return "BROWSER_DEMO"
