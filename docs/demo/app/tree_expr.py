"""
tree_expr.py
============
Safe formula language for Strategy Tree Builder node calculations, rule splits, leaf
scores and custom metrics. Python-like syntax, evaluated vectorised over a DataFrame,
and translatable to SQL and to row-level Python for the exporters.

    debt_to_income_pct * 1.1 + 5
    min(vantage_score, fico_score_proxy)
    where(thin_file_flag == 1, policy_score_existing - 20, policy_score_existing)
    state in ['CA', 'NY'] and not isnull(collateral_value)
    600 if delinquencies_24m_count > 0 else 650
    col('column with spaces') / 12

Only literals, column / calculation names, arithmetic, comparisons, and/or/not, in /
not in lists, ``a if c else b`` and the functions in FUNCTIONS are allowed: no attribute
access, subscripts, lambdas or imports. Comparisons with a missing value are False.
"""

import ast
import math

import numpy as np
import pandas as pd

FUNCTIONS = {
    "min": "smallest of the arguments (element-wise)",
    "max": "largest of the arguments (element-wise)",
    "abs": "absolute value",
    "round": "round(x, digits)",
    "floor": "round down", "ceil": "round up",
    "log": "natural logarithm", "exp": "exponential", "sqrt": "square root",
    "clip": "clip(x, low, high)",
    "where": "where(condition, value_if_true, value_if_false)",
    "coalesce": "first non-missing argument",
    "isnull": "True when missing", "notnull": "True when not missing",
    "between": "between(x, low, high) inclusive",
    "col": "col('name') – a column whose name is not a valid identifier",
}
_BINOPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.FloorDiv: "//", ast.Mod: "%", ast.Pow: "**"}
_CMPOPS = {ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!="}


class ExprError(ValueError):
    pass


def parse(expr):
    if expr is None or not str(expr).strip():
        raise ExprError("Empty formula.")
    try:
        tree = ast.parse(str(expr).strip(), mode="eval")
    except SyntaxError as e:
        raise ExprError(f"Syntax error in '{expr}': {e.msg}") from None
    _check(tree.body)
    return tree.body


def _check(n):
    if isinstance(n, ast.Constant):
        if not isinstance(n.value, (int, float, str, bool, type(None))):
            raise ExprError("Unsupported literal.")
    elif isinstance(n, ast.Name):
        pass
    elif isinstance(n, ast.BinOp):
        if type(n.op) not in _BINOPS:
            raise ExprError("Unsupported operator.")
        _check(n.left), _check(n.right)
    elif isinstance(n, ast.UnaryOp):
        if not isinstance(n.op, (ast.USub, ast.UAdd, ast.Not)):
            raise ExprError("Unsupported unary operator.")
        _check(n.operand)
    elif isinstance(n, ast.BoolOp):
        for v in n.values:
            _check(v)
    elif isinstance(n, ast.Compare):
        _check(n.left)
        for op, c in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                if not isinstance(c, (ast.List, ast.Tuple, ast.Set)) or not all(isinstance(e, ast.Constant) for e in c.elts):
                    raise ExprError("'in' needs a list of literal values, e.g. state in ['CA', 'NY'].")
            elif type(op) not in _CMPOPS:
                raise ExprError("Unsupported comparison.")
            else:
                _check(c)
    elif isinstance(n, ast.IfExp):
        _check(n.test), _check(n.body), _check(n.orelse)
    elif isinstance(n, ast.Call):
        if not isinstance(n.func, ast.Name) or n.func.id not in FUNCTIONS or n.keywords:
            raise ExprError(f"Unknown function. Allowed: {', '.join(sorted(FUNCTIONS))}.")
        if n.func.id == "col" and (len(n.args) != 1 or not isinstance(n.args[0], ast.Constant)):
            raise ExprError("col() takes one quoted column name.")
        for a in n.args:
            _check(a)
    else:
        raise ExprError(f"Unsupported syntax: {type(n).__name__}.")


def names(expr):
    """Column / calculation names a formula reads."""
    out = []
    for n in ast.walk(parse(expr)):
        nm = None
        if isinstance(n, ast.Name) and n.id not in ("True", "False", "None"):
            nm = n.id
        elif isinstance(n, ast.Call) and n.func.id == "col":
            nm = n.args[0].value
        if nm and nm not in out and not (isinstance(n, ast.Name) and nm in FUNCTIONS):
            out.append(nm)
    return out


# ════════════════════════════════════════════════════════════════════
# vectorised evaluation
# ════════════════════════════════════════════════════════════════════

def evaluate(expr, env, index):
    """Evaluate over ``env`` (name -> Series aligned on ``index``). Returns a Series."""
    v = _ev(parse(expr), env, index)
    if not isinstance(v, pd.Series):
        v = pd.Series([v] * len(index), index=index)
    return v


def _num(s):
    if isinstance(s, pd.Series):
        if pd.api.types.is_bool_dtype(s):
            return s.astype(float)
        if pd.api.types.is_numeric_dtype(s):
            return s.astype(float)
        return pd.to_numeric(s, errors="coerce")
    return s


def _bool(s):
    if isinstance(s, pd.Series):
        if pd.api.types.is_bool_dtype(s):
            return s
        return s.fillna(0).astype(float) != 0 if pd.api.types.is_numeric_dtype(s) else s.notna() & (s.astype(str) != "")
    return bool(s)


def _lookup(name, env):
    if name in env:
        v = env[name]
        return v() if callable(v) else v
    if name == "True":
        return True
    if name == "False":
        return False
    if name == "None":
        return np.nan
    raise ExprError(f"Unknown column or calculation '{name}'.")


def _ev(n, env, idx):
    if isinstance(n, ast.Constant):
        return np.nan if n.value is None else n.value
    if isinstance(n, ast.Name):
        return _lookup(n.id, env)
    if isinstance(n, ast.BinOp):
        a, b = _num(_ev(n.left, env, idx)), _num(_ev(n.right, env, idx))
        op = _BINOPS[type(n.op)]
        with np.errstate(all="ignore"):
            if op == "+":
                r = a + b
            elif op == "-":
                r = a - b
            elif op == "*":
                r = a * b
            elif op == "/":
                r = a / b
            elif op == "//":
                r = a // b
            elif op == "%":
                r = a % b
            else:
                r = a ** b
        if isinstance(r, pd.Series):
            r = r.replace([np.inf, -np.inf], np.nan)
        return r
    if isinstance(n, ast.UnaryOp):
        v = _ev(n.operand, env, idx)
        if isinstance(n.op, ast.Not):
            return ~_bool(v) if isinstance(v, pd.Series) else not _bool(v)
        v = _num(v)
        return -v if isinstance(n.op, ast.USub) else v
    if isinstance(n, ast.BoolOp):
        vals = [_bool(_ev(v, env, idx)) for v in n.values]
        out = vals[0]
        for v in vals[1:]:
            out = (out & v) if isinstance(n.op, ast.And) else (out | v)
        return out
    if isinstance(n, ast.Compare):
        left = _ev(n.left, env, idx)
        result = None
        for op, comp in zip(n.ops, n.comparators):
            if isinstance(op, (ast.In, ast.NotIn)):
                vals = [e.value for e in comp.elts]
                s = left if isinstance(left, pd.Series) else pd.Series([left] * len(idx), index=idx)
                m = s.astype(object).map(_canon).isin({_canon(v) for v in vals})
                r = m if isinstance(op, ast.In) else (~m & s.notna())
                right = None
            else:
                right = _ev(comp, env, idx)
                r = _compare(left, right, _CMPOPS[type(op)], idx)
            result = r if result is None else (result & r)
            left = right
        return result
    if isinstance(n, ast.IfExp):
        c = _bool(_ev(n.test, env, idx))
        a, b = _ev(n.body, env, idx), _ev(n.orelse, env, idx)
        return _where(c, a, b, idx)
    if isinstance(n, ast.Call):
        return _call(n.func.id, n.args, env, idx)
    raise ExprError("Unsupported expression.")


def _canon(z):
    if z is None or (isinstance(z, float) and math.isnan(z)):
        return None
    try:
        f = float(z)
        if f.is_integer():
            return str(int(f))
        return repr(f)
    except (TypeError, ValueError):
        return str(z).strip()


def _series(v, idx):
    return v if isinstance(v, pd.Series) else pd.Series([v] * len(idx), index=idx)


def _compare(a, b, op, idx):
    a, b = _series(a, idx), _series(b, idx)
    an, bn = _num(a), _num(b)
    numeric = an.notna().sum() >= a.notna().sum() and bn.notna().sum() >= b.notna().sum()
    if not numeric:
        an, bn = a.astype(object).map(_canon), b.astype(object).map(_canon)
        valid = a.notna() & b.notna()
        if op == "==":
            return (an == bn) & valid
        if op == "!=":
            return (an != bn) & valid
        an, bn = a.astype(str), b.astype(str)
    else:
        valid = an.notna() & bn.notna()
    with np.errstate(invalid="ignore"):
        r = {"<": an < bn, "<=": an <= bn, ">": an > bn, ">=": an >= bn, "==": an == bn, "!=": an != bn}[op]
    return r & valid


def _where(c, a, b, idx):
    c = _series(c, idx).astype(bool)
    a, b = _series(a, idx), _series(b, idx)
    return a.where(c, b)


def _call(fn, args, env, idx):
    if fn == "col":
        return _lookup(args[0].value, env)
    vals = [_ev(a, env, idx) for a in args]
    if fn in ("min", "max"):
        if not vals:
            raise ExprError(f"{fn}() needs arguments.")
        df = pd.concat([_num(_series(v, idx)) for v in vals], axis=1)
        return df.min(axis=1, skipna=True) if fn == "min" else df.max(axis=1, skipna=True)
    if fn == "where":
        if len(vals) != 3:
            raise ExprError("where(condition, a, b) takes 3 arguments.")
        return _where(_bool(_series(vals[0], idx)), vals[1], vals[2], idx)
    if fn == "coalesce":
        out = _series(vals[0], idx)
        for v in vals[1:]:
            out = out.where(out.notna(), _series(v, idx))
        return out
    if fn == "isnull":
        return _series(vals[0], idx).isna()
    if fn == "notnull":
        return _series(vals[0], idx).notna()
    if fn == "between":
        if len(vals) != 3:
            raise ExprError("between(x, low, high) takes 3 arguments.")
        return _compare(vals[0], vals[1], ">=", idx) & _compare(vals[0], vals[2], "<=", idx)
    if fn == "clip":
        if len(vals) != 3:
            raise ExprError("clip(x, low, high) takes 3 arguments.")
        return _num(_series(vals[0], idx)).clip(lower=vals[1], upper=vals[2])
    if fn == "round":   # half away from zero, like SQL ROUND
        f = 10.0 ** (int(vals[1]) if len(vals) > 1 else 0)
        x = _num(_series(vals[0], idx))
        return np.sign(x) * np.floor(x.abs() * f + 0.5 + 1e-9) / f
    x = _num(_series(vals[0], idx))
    with np.errstate(all="ignore"):
        r = {"abs": np.abs, "floor": np.floor, "ceil": np.ceil, "log": np.log, "exp": np.exp, "sqrt": np.sqrt}[fn](x)
    return pd.Series(r, index=idx).replace([np.inf, -np.inf], np.nan)


# ════════════════════════════════════════════════════════════════════
# translation (exporters)
# ════════════════════════════════════════════════════════════════════

# SQL dialects for the exporters / in-database backtest. "ansi" (double-quoted identifiers) covers
# Snowflake, PostgreSQL, Redshift, Oracle, DuckDB, SQLite...; BigQuery and MySQL quote with backticks.
SQL_DIALECTS = {
    "ansi": {"quote": '"', "string": "VARCHAR", "lit_esc": "''"},
    "bigquery": {"quote": "`", "string": "STRING", "lit_esc": "\\'"},
    "mysql": {"quote": "`", "string": "CHAR", "lit_esc": "''"},
}
_DIALECT = ["ansi"]


class sql_dialect(object):
    """with sql_dialect("bigquery"): ... – SQL generated inside uses that dialect."""

    def __init__(self, name):
        self.name = name if name in SQL_DIALECTS else "ansi"

    def __enter__(self):
        self.prev = _DIALECT[0]
        _DIALECT[0] = self.name
        return self

    def __exit__(self, *exc):
        _DIALECT[0] = self.prev


def dialect():
    return SQL_DIALECTS[_DIALECT[0]]


def _q(c):
    qc = dialect()["quote"]
    return qc + str(c).replace(qc, qc + qc) + qc


def sql_str(v):
    return "'" + str(v).replace("'", dialect()["lit_esc"]) + "'"


def to_sql(expr, subst=None):
    """SQL expression; ``subst`` maps calculation names to already-translated SQL."""
    subst = subst or {}

    def lit(v):
        if v is None:
            return "NULL"
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            return repr(v)
        return sql_str(v)

    def ref(name):
        return f"({subst[name]})" if name in subst else _q(name)

    def t(n):
        if isinstance(n, ast.Constant):
            return lit(n.value)
        if isinstance(n, ast.Name):
            return {"True": "TRUE", "False": "FALSE", "None": "NULL"}.get(n.id) or ref(n.id)
        if isinstance(n, ast.BinOp):
            op = _BINOPS[type(n.op)]
            if op == "**":
                return f"POWER({t(n.left)}, {t(n.right)})"
            if op == "//":
                return f"FLOOR({t(n.left)} / NULLIF({t(n.right)}, 0))"
            if op == "/":
                return f"({t(n.left)} / NULLIF({t(n.right)}, 0))"
            if op == "%":
                return f"MOD({t(n.left)}, {t(n.right)})"
            return f"({t(n.left)} {op} {t(n.right)})"
        if isinstance(n, ast.UnaryOp):
            return f"(NOT {t(n.operand)})" if isinstance(n.op, ast.Not) else f"(-{t(n.operand)})"
        if isinstance(n, ast.BoolOp):
            j = " AND " if isinstance(n.op, ast.And) else " OR "
            return "(" + j.join(t(v) for v in n.values) + ")"
        if isinstance(n, ast.Compare):
            parts, left = [], n.left
            for op, c in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    vals = ", ".join(lit(e.value) for e in c.elts)
                    parts.append(f"{t(left)} {'IN' if isinstance(op, ast.In) else 'NOT IN'} ({vals})")
                else:
                    o = _CMPOPS[type(op)]
                    parts.append(f"{t(left)} {'=' if o == '==' else '<>' if o == '!=' else o} {t(c)}")
                left = c
            return "(" + " AND ".join(parts) + ")"
        if isinstance(n, ast.IfExp):
            return f"(CASE WHEN {t(n.test)} THEN {t(n.body)} ELSE {t(n.orelse)} END)"
        fn, a = n.func.id, n.args
        if fn == "col":
            return ref(a[0].value)
        if fn == "min":
            return f"LEAST({', '.join(t(x) for x in a)})"
        if fn == "max":
            return f"GREATEST({', '.join(t(x) for x in a)})"
        if fn == "where":
            return f"(CASE WHEN {t(a[0])} THEN {t(a[1])} ELSE {t(a[2])} END)"
        if fn == "coalesce":
            return f"COALESCE({', '.join(t(x) for x in a)})"
        if fn == "isnull":
            return f"({t(a[0])} IS NULL)"
        if fn == "notnull":
            return f"({t(a[0])} IS NOT NULL)"
        if fn == "between":
            return f"({t(a[0])} BETWEEN {t(a[1])} AND {t(a[2])})"
        if fn == "clip":
            return f"LEAST(GREATEST({t(a[0])}, {t(a[1])}), {t(a[2])})"
        if fn == "round":
            return f"ROUND({t(a[0])}, {t(a[1]) if len(a) > 1 else 0})"
        return f"{ {'abs': 'ABS', 'floor': 'FLOOR', 'ceil': 'CEIL', 'log': 'LN', 'exp': 'EXP', 'sqrt': 'SQRT'}[fn] }({t(a[0])})"

    return t(parse(expr))


PY_HELPERS = '''import math


def _m(v):
    return v is None or (isinstance(v, float) and math.isnan(v))


def _n(v):
    try:
        return None if _m(v) else float(v)
    except (TypeError, ValueError):
        return None


def _cmp(a, op, b):
    if _m(a) or _m(b):
        return False
    na, nb = _n(a), _n(b)
    if na is not None and nb is not None:
        a, b = na, nb
    else:
        a, b = str(a), str(b)
    return {"<": a < b, "<=": a <= b, ">": a > b, ">=": a >= b, "==": a == b, "!=": a != b}[op]


def _canon(z):
    f = _n(z)
    if f is not None and float(f).is_integer():
        return str(int(f))
    return None if _m(z) else str(z).strip()


def _ar(op, a, b):
    a, b = _n(a), _n(b)
    if a is None or b is None:
        return None
    try:
        if op == "+":
            return a + b
        if op == "-":
            return a - b
        if op == "*":
            return a * b
        if op == "/":
            return a / b if b else None
        if op == "//":
            return a // b if b else None
        if op == "%":
            return a % b if b else None
        return a ** b
    except (OverflowError, ValueError, ZeroDivisionError):
        return None


def _f(fn, *a):
    vals = [_n(x) for x in a]
    if fn in ("min", "max"):
        v = [x for x in vals if x is not None]
        return (min(v) if fn == "min" else max(v)) if v else None
    if fn == "coalesce":
        return next((x for x in a if not _m(x)), None)
    if fn == "clip":
        return None if vals[0] is None else min(max(vals[0], vals[1]), vals[2])
    if fn == "round":   # half away from zero, like SQL ROUND
        if vals[0] is None:
            return None
        f = 10.0 ** (int(vals[1]) if len(vals) > 1 else 0)
        return math.copysign(math.floor(abs(vals[0]) * f + 0.5 + 1e-9) / f, vals[0])
    x = vals[0]
    if x is None:
        return None
    try:
        return {"abs": abs, "floor": math.floor, "ceil": math.ceil, "log": math.log, "exp": math.exp,
                "sqrt": math.sqrt}[fn](x)
    except (ValueError, OverflowError):
        return None
'''


def to_python(expr, var="row", subst=None):
    """Row-level Python expression using PY_HELPERS; ``subst`` maps calc names to local variables."""
    subst = subst or {}

    def ref(name):
        return subst.get(name) or f"{var}.get({name!r})"

    def t(n):
        if isinstance(n, ast.Constant):
            return repr(n.value)
        if isinstance(n, ast.Name):
            return {"True": "True", "False": "False", "None": "None"}.get(n.id) or ref(n.id)
        if isinstance(n, ast.BinOp):
            return f"_ar({_BINOPS[type(n.op)]!r}, {t(n.left)}, {t(n.right)})"
        if isinstance(n, ast.UnaryOp):
            return f"(not {t(n.operand)})" if isinstance(n.op, ast.Not) else f"_ar('*', -1, {t(n.operand)})"
        if isinstance(n, ast.BoolOp):
            j = " and " if isinstance(n.op, ast.And) else " or "
            return "(" + j.join(f"bool({t(v)})" for v in n.values) + ")"
        if isinstance(n, ast.Compare):
            parts, left = [], n.left
            for op, c in zip(n.ops, n.comparators):
                if isinstance(op, (ast.In, ast.NotIn)):
                    vals = {_canon(e.value) for e in c.elts}
                    p = f"(_canon({t(left)}) in {vals!r})"
                    parts.append(p if isinstance(op, ast.In) else f"(not _m({t(left)}) and not {p})")
                else:
                    parts.append(f"_cmp({t(left)}, {_CMPOPS[type(op)]!r}, {t(c)})")
                left = c
            return "(" + " and ".join(parts) + ")"
        if isinstance(n, ast.IfExp):
            return f"({t(n.body)} if {t(n.test)} else {t(n.orelse)})"
        fn, a = n.func.id, n.args
        if fn == "col":
            return ref(a[0].value)
        if fn == "where":
            return f"({t(a[1])} if {t(a[0])} else {t(a[2])})"
        if fn == "isnull":
            return f"_m({t(a[0])})"
        if fn == "notnull":
            return f"(not _m({t(a[0])}))"
        if fn == "between":
            return f"(_cmp({t(a[0])}, '>=', {t(a[1])}) and _cmp({t(a[0])}, '<=', {t(a[2])}))"
        return f"_f({fn!r}, {', '.join(t(x) for x in a)})"

    return t(parse(expr))
