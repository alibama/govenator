"""python3 tests/test_scout.py   (or pytest)"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import requests  # noqa: E402

import scout as S  # noqa: E402
import textify  # noqa: E402

UP = "/mnt/user-data/uploads"


def test_names():
    assert S.split_name("Charlottesville city") == ("Charlottesville", "city")
    assert S.split_name("Albemarle County") == ("Albemarle", "county")
    assert S.split_name("Nashville-Davidson metropolitan government (balance)") == ("Nashville-Davidson", "city")
    assert S.split_name("Clinton charter township") == ("Clinton", "township")
    assert S.display_name("Blacksburg", "town", "place", "VA") == "Town of Blacksburg, VA"
    assert S.display_name("Orleans", "parish", "county", "LA") == "Orleans Parish, LA"
    assert S.no_government("county", "CT", "planning region", "Capitol Planning Region")
    assert S.no_government("county", "AK", "census area", "Bethel Census Area")
    assert not S.no_government("county", "VA", "county", "Albemarle County")


def test_guesses():
    g = S.guess_urls({"base": "Charlottesville", "type": "city", "state": "VA", "kind": "place"})
    assert "charlottesville.gov" in g and "cityofcharlottesville.gov" in g and "ci.charlottesville.va.us" in g
    g = S.guess_urls({"base": "St. Louis", "type": "county", "state": "MO", "kind": "county"})
    assert "stlouiscounty.gov" in g and "co.stlouis.mo.us" in g


def test_link_ranking_prefers_directories_and_skips_noise():
    links = [("https://x.gov/directory", "Staff Directory"), ("https://x.gov/news", "News"),
             ("https://x.gov/pay", "Pay your bill"), ("https://x.gov/government/council", "City Council"),
             ("https://facebook.com/x", "Facebook"), ("https://x.gov/docs/a.pdf", "Directory (PDF)"),
             ("https://x.gov/departments", "Departments")]
    ranked = [u for _, u, _ in S.rank_links(links, "https://www.x.gov/")]
    assert ranked[0] == "https://x.gov/directory"
    assert "https://x.gov/news" not in ranked and "https://facebook.com/x" not in ranked
    assert not any(u.endswith(".pdf") for u in ranked)
    assert set(ranked) == {"https://x.gov/directory", "https://x.gov/government/council", "https://x.gov/departments"}


def test_real_pages_score_sensibly():
    if not os.path.exists(f"{UP}/popo.txt"):
        return
    police = textify.analyze(open(f"{UP}/popo.txt", errors="replace").read(), "https://www.charlottesville.gov/x")
    index = textify.analyze(open(f"{UP}/staff.txt", errors="replace").read(), "https://www.charlottesville.gov/y")
    assert police["platform"] == "CivicPlus" and police["metrics"]["emails"] >= 20 and police["score"] >= 80
    assert police["title"].startswith("Staff Directory")
    assert 30 <= index["score"] < police["score"]
    assert any(l.startswith("    - ") for l in index["text"].splitlines())   # nested department options survive


def test_obfuscated_email_is_decoded():
    html = '<table><tr><td>Pat</td><td><script>var w = "pat"; var x = "town.gov"; document.write("x")</script></td></tr></table>'
    a = textify.analyze(html, "https://town.gov/")
    assert a["metrics"]["emails"] == 1 and "<pat@town.gov>" in a["text"]


def test_wikidata_matching(monkeypatch=None):
    with tempfile.TemporaryDirectory() as d:
        conn = S.open_db(os.path.join(d, "t.db"))
        conn.execute("INSERT INTO jurisdiction(jid,kind,geoid,state,base,gnis,stage) VALUES('P1','place','5114968','VA','Charlottesville','1498463','new')")
        conn.execute("INSERT INTO jurisdiction(jid,kind,geoid,state,base,stage) VALUES('C51003','county','51003','VA','Albemarle','new')")
        conn.commit()

        class R:
            status_code = 200
            headers = {}

            def __init__(self, rows):
                self.rows = rows

            def json(self):
                return {"results": {"bindings": self.rows}}

        def fake_post(self, url, data=None, timeout=None):
            if "P590" in data["query"]:
                return R([{"item": {"value": "http://www.wikidata.org/entity/Q123766"}, "code": {"value": "1498463"},
                           "site": {"value": "https://www.charlottesville.gov/"}},
                          {"item": {"value": "http://www.wikidata.org/entity/Q123766"}, "code": {"value": "1498463"},
                           "site": {"value": "http://charlottesville.gov"}}])
            return R([{"item": {"value": "http://www.wikidata.org/entity/Q111"}, "code": {"value": "51003"}}])

        orig, S.time.sleep = S.time.sleep, lambda s: None
        old = requests.Session.post
        requests.Session.post = fake_post
        try:
            found = S.load_wikidata(conn)
        finally:
            requests.Session.post, S.time.sleep = old, orig
        rows = {r["jid"]: dict(r) for r in conn.execute("SELECT jid, qid, wd_website FROM jurisdiction")}
        assert found == 1
        assert rows["P1"] == {"jid": "P1", "qid": "Q123766", "wd_website": "https://www.charlottesville.gov/"}
        assert rows["C51003"]["qid"] == "Q111" and rows["C51003"]["wd_website"] is None   # QID even without a website
        cand = conn.execute("SELECT source, confidence FROM candidate WHERE jid='P1'").fetchone()
        assert tuple(cand) == ("wikidata", 0.9)


def test_robots_rules_follow_rfc9309():
    net = S.Net(delay=0)
    for status, body, allowed in [(200, "User-agent: *\nDisallow: /private\n", False),
                                  (404, "", True), (503, "", False)]:
        net.robots = {}
        net._raw = lambda url, accept_xml=False, s=status, b=body: S.Resp(url, s, url, b)
        rp = net.robots_for("https://t.gov/private/x")
        assert rp.can_fetch(S.UA_TOKEN, "https://t.gov/private/x") == allowed, (status, allowed)


if __name__ == "__main__":
    n = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
