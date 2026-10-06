"""
store — one database interface for a local SQLite file or a shared Turso database.

    conn = store.connect("scout.db")                                  # local file
    conn = store.connect("libsql://scout-me.turso.io", token="...")   # Turso

Both return an object with the sqlite3 surface this project uses: execute(),
executemany(), executescript(), commit(), `with conn:`, rows that work as
row["col"], row[0], dict(row) and tuple(row), plus cursor.lastrowid/rowcount.

Turso is spoken over its HTTP protocol (Hrana v2 "pipeline") with `requests`
only, so it runs anywhere, including Streamlit Community Cloud, without native
drivers. Each execute() is one HTTPS round trip; executemany() sends up to
`BATCH` statements per request inside one transaction.
"""
import base64
import re
import sqlite3
import threading

import requests

BATCH = 200


def is_remote(url):
    return bool(url) and url.startswith(("libsql://", "https://", "http://", "wss://"))


def connect(url, token=None):
    if is_remote(url):
        return HranaConnection(url, token)
    conn = sqlite3.connect(url, timeout=60, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def df(conn, sql, args=()):
    """Query -> pandas DataFrame for either backend."""
    import pandas as pd
    cur = conn.execute(sql, tuple(args))
    cols = [d[0] for d in (cur.description or [])]
    return pd.DataFrame([tuple(r) for r in cur.fetchall()], columns=cols)


def split_script(sql):
    stmts = []
    for s in sql.split(";"):
        lines = [ln for ln in s.splitlines() if not ln.strip().startswith("--")]
        s = "\n".join(lines).strip()
        if s:
            stmts.append(s)
    return stmts


# ---------------------------------------------------------------------------
# Turso / libSQL over HTTP
# ---------------------------------------------------------------------------
class Row(tuple):
    """Tuple that also supports row['col'] and dict(row), like sqlite3.Row."""

    def __new__(cls, values, keys):
        r = super().__new__(cls, values)
        r._keys = keys
        r._idx = {k: i for i, k in enumerate(keys)}
        return r

    def __getitem__(self, k):
        if isinstance(k, str):
            return tuple.__getitem__(self, self._idx[k])
        return tuple.__getitem__(self, k)

    def keys(self):
        return list(self._keys)


class Cursor:
    def __init__(self, cols, rows, lastrowid=None, rowcount=-1):
        self.description = [(c, None, None, None, None, None, None) for c in cols] if cols else None
        self._rows = [Row(r, cols) for r in rows]
        self.lastrowid, self.rowcount = lastrowid, rowcount

    def fetchone(self):
        return self._rows.pop(0) if self._rows else None

    def fetchall(self):
        rows, self._rows = self._rows, []
        return rows

    def __iter__(self):
        return iter(self.fetchall())


def _enc(v):
    if v is None:
        return {"type": "null"}
    if isinstance(v, bool):
        return {"type": "integer", "value": str(int(v))}
    if isinstance(v, int):
        return {"type": "integer", "value": str(v)}
    if isinstance(v, float):
        return {"type": "float", "value": v}
    if isinstance(v, (bytes, bytearray, memoryview)):
        return {"type": "blob", "base64": base64.b64encode(bytes(v)).decode().rstrip("=")}
    return {"type": "text", "value": str(v)}


def _dec(v):
    t = v.get("type")
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        b = v.get("base64", "")
        return base64.b64decode(b + "=" * (-len(b) % 4))
    return v.get("value")


_NAMED = re.compile(r"(?<![:\w]):([A-Za-z_]\w*)")


def _positional(sql, params):
    """Rewrite :name parameters to ? so every statement can use positional args."""
    if isinstance(params, dict):
        names = _NAMED.findall(sql)
        return _NAMED.sub("?", sql), [params[n] for n in names]
    return sql, list(params or ())


class TursoError(RuntimeError):
    pass


class HranaConnection:
    def __init__(self, url, token=None, timeout=60):
        if url.startswith("libsql://"):
            url = "https://" + url[len("libsql://"):]
        elif url.startswith("wss://"):
            url = "https://" + url[len("wss://"):]
        self.base = url.rstrip("/")
        self.token = token
        self.timeout = timeout
        self.local = threading.local()

    def _session(self):
        if not hasattr(self.local, "s"):
            s = requests.Session()
            if self.token:
                s.headers["Authorization"] = f"Bearer {self.token}"
            self.local.s = s
        return self.local.s

    def _pipeline(self, stmts):
        reqs = [{"type": "execute", "stmt": {"sql": sql, "args": [_enc(a) for a in args]}} for sql, args in stmts]
        reqs.append({"type": "close"})
        r = self._session().post(self.base + "/v2/pipeline", json={"baton": None, "requests": reqs},
                                 timeout=self.timeout)
        if r.status_code != 200:
            raise TursoError(f"Turso HTTP {r.status_code}: {r.text[:300]}")
        out = []
        for res in r.json().get("results", [])[: len(stmts)]:
            if res.get("type") == "error":
                raise TursoError(res.get("error", {}).get("message", "unknown Turso error"))
            out.append(res["response"]["result"])
        return out

    def execute(self, sql, params=()):
        sql, args = _positional(sql, params)
        res = self._pipeline([(sql, args)])[0]
        cols = [c.get("name") for c in res.get("cols", [])]
        rows = [[_dec(v) for v in row] for row in res.get("rows", [])]
        lid = res.get("last_insert_rowid")
        return Cursor(cols, rows, int(lid) if lid not in (None, "") else None, res.get("affected_row_count", -1))

    def executemany(self, sql, seq):
        stmts = [_positional(sql, p) for p in seq]
        n = 0
        for i in range(0, len(stmts), BATCH):
            chunk = stmts[i:i + BATCH]
            self._pipeline([("BEGIN", [])] + chunk + [("COMMIT", [])])
            n += len(chunk)
        return Cursor([], [], None, n)

    def executescript(self, sql):
        stmts = [(s, []) for s in split_script(sql) if not s.upper().startswith("PRAGMA")]
        for i in range(0, len(stmts), BATCH):
            self._pipeline(stmts[i:i + BATCH])

    # sqlite3 compatibility: every statement autocommits on the server
    def commit(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False
