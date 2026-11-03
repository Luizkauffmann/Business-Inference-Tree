"""Routes browser 'requests' of the demo to the unchanged webapp backends (Pyodide)."""
import io
import json
import re
import threading
import urllib.parse

import pandas as pd

import dataiku
import flask


class _SyncThread(object):
    """Pyodide has no threads: background warm-ups simply run inline."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None, **_):
        self._t, self._a, self._k = target, args, kwargs or {}

    def start(self):
        try:
            self._t(*self._a, **self._k)
        except Exception:
            pass


threading.Thread = _SyncThread


class App(object):
    def __init__(self):
        self.routes = {}

    def route(self, path, methods=None):
        def deco(f):
            self.routes[path] = f
            return f
        return deco


APPS = {}


def register(name, module):
    app = App()
    __import__(module).register(app)
    APPS[name] = app


def dispatch(name, path, query, body):
    """-> (text, status, content type)."""
    f = APPS[name].routes.get(path)
    if f is None:
        return json.dumps({"error": f"Unknown route {path}"}), 404, "application/json"
    flask.request._json = json.loads(body) if body else None
    flask.request.args = dict(urllib.parse.parse_qsl(query or ""))
    try:
        rv = f()
    except Exception as e:
        return json.dumps({"error": str(e)}), 500, "application/json"
    status = 200
    if isinstance(rv, tuple):
        rv, status = rv[0], rv[1]
    if isinstance(rv, flask.JsonReply):
        return json.dumps(rv.payload, default=str), status, "application/json"
    if isinstance(rv, flask.Response):
        return rv.body, rv.status, rv.mimetype
    return json.dumps(rv, default=str), status, "application/json"


def load_csv(name, text):
    """Add a CSV as a dataset; returns the dataset name actually used."""
    name = re.sub(r"[^A-Za-z0-9_]+", "_", name).strip("_") or "uploaded"
    dataiku.TABLES[name] = pd.read_csv(io.StringIO(text), low_memory=False)
    return name


def datasets():
    return json.dumps(sorted(dataiku.TABLES))
