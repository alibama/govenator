"""Manual input, exports and the bridge.   python3 tests/test_flows.py"""
import json
import os
import socket
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "bridge"))
import requests  # noqa: E402

import exports as E  # noqa: E402
import scout as S  # noqa: E402

TREE = {"name": "City of Exampleville", "kind": "root", "site": "exampleville.gov", "children": [
    {"name": "Police Department", "kind": "unit", "unit_kind": "department", "seen_by": ["qwen3.6", "granite"],
     "contacts": {"phone": "434-555-2000", "email": "police@exampleville.gov"}, "children": [
        {"name": "Patrol Division", "kind": "unit", "unit_kind": "division", "seen_by": ["qwen3.6"], "contacts": {}, "children": []},
        {"name": "Chief of Police", "kind": "position", "holder": "Riley Shaw", "seen_by": ["qwen3.6"], "children": []}]},
    {"name": "Town Clerk", "kind": "position", "holder": "Avery Lin", "children": []}]}


def fresh():
    d = tempfile.mkdtemp()
    c = S.open_db(os.path.join(d, "t.db"))
    c.execute("INSERT INTO jurisdiction(jid,kind,state,base,type,display,qid,wd_website,stage,aland) "
              "VALUES('P1','place','VA','Exampleville','city','City of Exampleville, VA','Q123',NULL,'indexed',10)")
    c.execute("INSERT INTO jurisdiction(jid,kind,state,base,type,display,qid,wd_website,stage,aland) "
              "VALUES('P2','place','VA','Otherton','town','Town of Otherton, VA','Q456','https://other.gov/','indexed',5)")
    c.execute("INSERT INTO jurisdiction(jid,kind,state,base,type,display,stage,aland) "
              "VALUES('P3','place','VA','Nowhere','town','Town of Nowhere, VA','no_site',50)")
    c.execute("INSERT INTO jurisdiction(jid,kind,state,base,type,display,stage,aland) "
              "VALUES('P4','place','VA','Hidden','town','Town of Hidden, VA','blocked',40)")
    c.execute("INSERT INTO site(jid,url,source,status,confidence) VALUES('P1','https://exampleville.gov/','dotgov','found',0.94)")
    c.execute("INSERT INTO site(jid,url,source,status,confidence) VALUES('P2','https://www.otherton-va.gov/','guess','found',0.8)")
    c.execute("INSERT INTO site(jid,url,source,status) VALUES('P3',NULL,NULL,'not_found')")
    c.execute("INSERT INTO site(jid,url,source,status) VALUES('P4','https://hidden.gov/','dotgov','blocked')")
    c.execute("INSERT INTO account VALUES('P1','P2013','ExamplevilleVA','https://facebook.com/ExamplevilleVA','scout')")
    c.execute("INSERT INTO account VALUES('P1','P2002','evillegov','https://x.com/evillegov','scout')")
    c.commit()
    return c, d


def test_wikidata_jurisdiction_export():
    c, _ = fresh()
    r = E.wikidata_jurisdictions(c, 0.6)
    lines = r["text"].splitlines()
    assert lines[0].startswith('Q123\tP856\t"https://exampleville.gov/"\tS854\t"https://exampleville.gov/"\tS813\t+')
    assert any(l.startswith('Q123\tP2013\t"ExamplevilleVA"') for l in lines)
    assert any(l.startswith('Q123\tP2002\t"evillegov"') for l in lines)
    assert r["websites"] == 1 and r["accounts"] == 2
    # Wikidata already lists a different site for Otherton: flagged for review, not overwritten
    assert r["review"][0]["qid"] == "Q456" and not any(l.startswith("Q456\tP856") for l in lines)
    assert r["open_url"].startswith(E.WIKIDATA_QS)


def test_department_export_two_passes_and_resolution():
    c, _ = fresh()
    c.execute("INSERT INTO orgchart VALUES('exampleville.gov','P1','qwen3.6',?,'h','now','now')", (json.dumps(TREE),))
    c.commit()
    units = E.org_units(c)
    assert [u["name"] for u in units] == ["Police Department", "Patrol Division"]   # positions never exported
    p1 = E.wikidata_departments(c, 1)
    t = p1["text"]
    assert t.count("CREATE") == 2 and 'LAST\tLen\t"Police Department"' in t and "LAST\tP31\tQ327333" in t
    assert 'LAST\tP1329\t"+1-434-555-2000"' in t and 'LAST\tP968\t"mailto:police@exampleville.gov"' in t
    assert "LAST\tP131\tQ123" in t and "Riley Shaw" not in t
    assert E.wikidata_departments(c, 1, min_models=2)["text"].count("CREATE") == 1   # agreement filter

    class R:
        def __init__(self, rows): self.rows = rows
        def raise_for_status(self): pass
        def json(self): return {"results": {"bindings": self.rows}}

    def fake_post(self, url, data=None, timeout=None):
        assert "wdt:P131 wd:Q123" in data["query"]
        return R([{"item": {"value": "http://www.wikidata.org/entity/Q901"}, "label": {"value": "Exampleville Police Department"}},
                  {"item": {"value": "http://www.wikidata.org/entity/Q902"}, "label": {"value": "Patrol Division"}}])
    old_post, old_sleep = requests.Session.post, time.sleep
    requests.Session.post, time.sleep = fake_post, (lambda s: None)
    try:
        assert E.resolve_wikidata(c, "test") == 2
    finally:
        requests.Session.post, time.sleep = old_post, old_sleep
    assert E.wikidata_departments(c, 1)["text"] == ""          # both exist now: nothing to create
    p2 = E.wikidata_departments(c, 2)["text"].splitlines()
    assert p2 == [l for l in p2 if l.startswith("Q902\tP749\tQ901\t")] and len(p2) == 1   # top level: no P749 to the city


def test_lexipedia_requires_mapping_then_exports():
    c, _ = fresh()
    c.execute("INSERT INTO orgchart VALUES('exampleville.gov','P1','qwen3.6',?,'h','now','now')", (json.dumps(TREE),))
    c.commit()
    cfg = E.lexipedia_config(c)
    try:
        E.lexipedia(c, cfg, 1)
        raise AssertionError("should refuse without property IDs")
    except ValueError as e:
        assert "instance_of" in str(e)
    cfg["props"].update(instance_of="P1", part_of="P2", source_key="P3", wikidata_id="P4", phone="P5", reference_url="P6")
    cfg["classes"].update(jurisdiction="Q10", agency="Q11")
    E.save_lexipedia_config(c, cfg)
    cfg = E.lexipedia_config(c)
    t = E.lexipedia(c, cfg, 1)["text"]
    assert t.count("CREATE") == 3 and 'LAST\tP4\t"Q123"' in t and 'LAST\tP3\t"jur:P1"' in t
    assert 'LAST\tP5\t"+1-434-555-2000"\tS6\t"https://exampleville.gov/"' in t
    c.executemany("INSERT INTO xref VALUES('lexipedia',?,?)",
                  [("jur:P1", "Q500"), ("unit:exampleville.gov|police-department", "Q501"),
                   ("unit:exampleville.gov|police-department/patrol-division", "Q502")])
    c.commit()
    assert set(E.lexipedia(c, cfg, 2)["text"].splitlines()) == {
        'Q501\tP2\tQ500\tS6\t"https://exampleville.gov/"', 'Q502\tP2\tQ501\tS6\t"https://exampleville.gov/"'}


def test_needs_you_claims_and_answers():
    c, _ = fresh()
    for jid in ("P3", "P4"):
        S.score_jurisdiction(c, jid)
    a = S.claim_next(c, "anson", ["no site found", "blocked"])
    b = S.claim_next(c, "lane", ["no site found", "blocked"])
    assert a == "P3" and b == "P4"                                   # biggest first, never the same one
    assert S.claim_next(c, "third", ["no site found", "blocked"]) is None
    S.save_manual(c, "P3", "anson", "none", note="checked the county site")
    assert c.execute("SELECT label FROM quality WHERE jid='P3'").fetchone()[0] == "no website (confirmed)"
    S.save_manual(c, "P4", "lane", "website", website="hidden.gov", directory_url="https://hidden.gov/staff",
                  accounts_text="https://www.facebook.com/HiddenTownVA\nhttps://x.com/hiddenva",
                  pasted_text="Town Manager — Pat Lee — manager@hidden.gov — 540-555-1000\n"
                              "Town Clerk — Sam Roe — clerk@hidden.gov — 540-555-1001\nChief of Police — Kai Wu — 540-555-1002")
    assert c.execute("SELECT source, confidence FROM candidate WHERE jid='P4'").fetchone()[:] == ("human", 1.0)
    assert c.execute("SELECT url FROM seed_page WHERE jid='P4'").fetchone()[0] == "https://hidden.gov/staff"
    assert {r[0] for r in c.execute("SELECT pid FROM account WHERE jid='P4'")} == {"P2013", "P2002"}
    pg = c.execute("SELECT via, emails, phones, roles FROM page WHERE jid='P4'").fetchone()
    assert pg["via"] == "human" and pg["emails"] == 2 and pg["phones"] == 3 and pg["roles"] >= 3
    assert c.execute("SELECT label FROM quality WHERE jid='P4'").fetchone()[0] != "blocked"
    assert S.claim_next(c, "third", ["no site found", "blocked"]) is None   # both handled


def _free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


def test_bridge_delivers_pages_and_returns_org_charts():
    import bridge
    c, d = fresh()
    c.execute("INSERT INTO harvest_queue(jid,url,title,text,queued_at) VALUES('P1','https://exampleville.gov/directory',"
              "'Staff','# Staff\\nURL: https://exampleville.gov/directory\\n\\nName | Title\\nJordan Ames | City Manager','now')")
    c.commit()
    hdb, port = os.path.join(d, "harvest.db"), _free_port()
    daemon = os.path.join(HERE, "..", "..", "harvest", "daemon", "harvestd.py")
    p = subprocess.Popen([sys.executable, daemon, "--db", hdb, "--port", str(port), "--no-worker"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                requests.get(f"http://127.0.0.1:{port}/health", timeout=1); break
            except requests.RequestException:
                time.sleep(0.25)
        bridge.main(["--db", os.path.join(d, "t.db"), "--harvest", f"http://127.0.0.1:{port}", "--harvest-db", hdb, "--once"])
        got = requests.get(f"http://127.0.0.1:{port}/pages").json()
        assert [x["url"] for x in got] == ["https://exampleville.gov/directory"]
        assert c.execute("SELECT status FROM harvest_queue").fetchone()[0] == "sent"
        import sqlite3                     # Harvest finishes an org chart for the site
        h = sqlite3.connect(hdb)
        h.execute("INSERT INTO site_model(site, model, data) VALUES('exampleville.gov','qwen3.6',?)", (json.dumps(TREE),))
        h.commit(); h.close()
        bridge.main(["--db", os.path.join(d, "t.db"), "--harvest", f"http://127.0.0.1:{port}", "--harvest-db", hdb, "--once"])
        oc = c.execute("SELECT jid, model FROM orgchart WHERE site='exampleville.gov'").fetchone()
        assert tuple(oc) == ("P1", "qwen3.6")
        n = c.execute("SELECT COUNT(*) FROM orgchart").fetchone()[0]
        bridge.main(["--db", os.path.join(d, "t.db"), "--harvest", f"http://127.0.0.1:{port}", "--harvest-db", hdb, "--once"])
        assert c.execute("SELECT COUNT(*) FROM orgchart").fetchone()[0] == n   # unchanged chart isn't re-sent
    finally:
        p.terminate()


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); n += 1; print("ok", name)
    print(f"{n} passed")
