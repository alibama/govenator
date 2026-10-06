#!/usr/bin/env python3
"""
bridge — connects the shared Scout database (usually Turso) to Harvest + Ollama
running on your own machine.

  shared app ──"Queue for Harvest"──▶ harvest_queue (Turso)
                                          │  bridge (this script, on your Mac)
                                          ▼
                                   harvestd  ──▶ Ollama parses, organizes
                                          │
  shared app ◀── orgchart (Turso) ◀───────┘  bridge sends finished org charts back

    SCOUT_DB=libsql://scout-you.turso.io SCOUT_DB_TOKEN=... \\
      python3 bridge/bridge.py --harvest-db harvest/harvest.db

Run it next to `python3 harvest/daemon/harvestd.py`. --once does a single pass.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from urllib.parse import urlparse

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scout"))
import exports  # noqa: E402
import scout as S  # noqa: E402


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def host_key(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def deliver(db, harvest_url, limit=50):
    """Send queued pages to the local Harvest daemon."""
    rows = db.execute("SELECT id, url, title, text, queued_at FROM harvest_queue WHERE status='queued' "
                      "ORDER BY id LIMIT ?", (limit,)).fetchall()
    sent = 0
    for r in rows:
        try:
            resp = requests.post(harvest_url.rstrip("/") + "/pages", timeout=30,
                                 json={"url": r["url"], "title": r["title"] or "", "text": r["text"] or "",
                                       "captured_at": r["queued_at"]})
            if resp.ok:
                db.execute("UPDATE harvest_queue SET status='sent', sent_at=?, error=NULL WHERE id=?", (S.now_iso(), r["id"]))
                sent += 1
            else:
                db.execute("UPDATE harvest_queue SET error=? WHERE id=?", (f"HTTP {resp.status_code}: {resp.text[:200]}", r["id"]))
        except requests.RequestException as e:
            log(f"Harvest daemon not reachable at {harvest_url} ({type(e).__name__}); will retry")
            break
        db.commit()
    return sent


def collect(db, harvest_db):
    """Copy Harvest's latest org chart per site into the shared database."""
    if not os.path.exists(harvest_db):
        return 0
    h = sqlite3.connect(harvest_db, timeout=30)
    h.row_factory = sqlite3.Row
    latest = h.execute("""SELECT m.site, m.model, m.data, m.created_at FROM site_model m
                          JOIN (SELECT site, MAX(id) id FROM site_model GROUP BY site) x ON x.id=m.id""").fetchall()
    h.close()
    if not latest:
        return 0
    jid_by_site = {}
    for r in db.execute("SELECT jid, url FROM site WHERE url IS NOT NULL").fetchall():
        jid_by_site.setdefault(host_key(r["url"]), r["jid"])
    known = {r["site"]: r["data_hash"] for r in db.execute("SELECT site, data_hash FROM orgchart").fetchall()}
    pushed = 0
    for r in latest:
        jid = jid_by_site.get(r["site"])
        digest = exports.content_hash(r["data"])
        if not jid or known.get(r["site"]) == digest:
            continue
        db.execute("INSERT OR REPLACE INTO orgchart VALUES(?,?,?,?,?,?,?)",
                   (r["site"], jid, r["model"], r["data"], digest, r["created_at"], S.now_iso()))
        db.commit()
        pushed += 1
        tree = json.loads(r["data"])
        log(f"org chart for {r['site']} -> shared DB ({tree.get('stats', {}).get('units', '?')} units)")
    return pushed


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="Scout DB (default $SCOUT_DB; token from $SCOUT_DB_TOKEN)")
    ap.add_argument("--harvest", default=os.environ.get("HARVEST_URL", "http://127.0.0.1:8765"))
    ap.add_argument("--harvest-db", default=os.environ.get("HARVEST_DB",
                                                           os.path.join(HERE, "..", "harvest", "harvest.db")))
    ap.add_argument("--interval", type=float, default=30)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    db = S.open_db(a.db)
    log(f"bridge: {a.db or S.db_url()} <-> {a.harvest} ({os.path.abspath(a.harvest_db)})")
    while True:
        try:
            sent, pushed = deliver(db, a.harvest), collect(db, os.path.abspath(a.harvest_db))
            if sent or pushed:
                log(f"delivered {sent} page(s), returned {pushed} org chart(s)")
        except Exception as e:
            log(f"bridge error: {e}")
        if a.once:
            break
        time.sleep(a.interval)


if __name__ == "__main__":
    main()
