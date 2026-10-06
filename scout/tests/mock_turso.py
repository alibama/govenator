#!/usr/bin/env python3
"""A stand-in for Turso: speaks Hrana-over-HTTP v2 (/v2/pipeline) and runs the SQL on
a local SQLite file. Lets the whole stack be tested in 'Turso mode' without an account.
    python3 tests/mock_turso.py --port 9090 --db /tmp/turso.db [--token secret]"""
import argparse
import base64
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOCK = threading.Lock()


def dec(v):
    t = v["type"]
    if t == "null":
        return None
    if t == "integer":
        return int(v["value"])
    if t == "float":
        return float(v["value"])
    if t == "blob":
        b = v["base64"]
        return base64.b64decode(b + "=" * (-len(b) % 4))
    return v["value"]


def enc(x):
    if x is None:
        return {"type": "null"}
    if isinstance(x, int):
        return {"type": "integer", "value": str(x)}
    if isinstance(x, float):
        return {"type": "float", "value": x}
    if isinstance(x, bytes):
        return {"type": "blob", "base64": base64.b64encode(x).decode()}
    return {"type": "text", "value": str(x)}


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.server.token and self.headers.get("Authorization") != f"Bearer {self.server.token}":
            self.send_response(401); self.end_headers(); return
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        results = []
        with LOCK:
            db = self.server.conn
            for req in body["requests"]:
                if req["type"] == "close":
                    results.append({"type": "ok", "response": {"type": "close"}})
                    continue
                st = req["stmt"]
                try:
                    cur = db.execute(st["sql"], [dec(a) for a in st.get("args", [])])
                    cols = [{"name": d[0], "decltype": None} for d in (cur.description or [])]
                    rows = [[enc(v) for v in r] for r in cur.fetchall()]
                    results.append({"type": "ok", "response": {"type": "execute", "result": {
                        "cols": cols, "rows": rows, "affected_row_count": max(cur.rowcount, 0),
                        "last_insert_rowid": str(cur.lastrowid) if cur.lastrowid else None}}})
                    if db.in_transaction is False:
                        db.commit()
                except Exception as e:
                    if db.in_transaction:
                        db.rollback()
                    results.append({"type": "error", "error": {"message": f"SQLite error: {e}", "code": "SQLITE_ERROR"}})
        b = json.dumps({"baton": None, "base_url": None, "results": results}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=9090)
    ap.add_argument("--db", default="/tmp/turso.db")
    ap.add_argument("--token", default="")
    a = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), H)
    srv.conn = sqlite3.connect(a.db, check_same_thread=False, isolation_level=None)
    srv.token = a.token
    srv.serve_forever()
