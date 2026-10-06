"""
exports — QuickStatements for Wikidata (what govdirectory.org reads) and Lexipedia.

Jurisdiction level (Wikidata, one pass)
    official website (P856) where Wikidata has none, and the government's online
    accounts (X P2002, Facebook P2013, Instagram P2003, YouTube P2397/P11245,
    LinkedIn P4264, Bluesky P12361, Mastodon P4033, Telegram P3789), each with a
    reference: reference URL (S854) + retrieved (S813).

Departments (from Harvest org charts; Wikidata or Lexipedia, two passes)
    pass 1  CREATE one item per unit not already known
    resolve look up the new (and any pre-existing) QIDs  -> xref table
    pass 2  link each unit to its parent
Office holders are never exported: positions are people's jobs, not agencies.
"""
import hashlib
import json
import re
import time
from urllib.parse import quote

import requests

WIKIDATA_QS = "https://quickstatements.toolforge.org/#/v1="
WDQS = "https://query.wikidata.org/sparql"
GOVERNMENT_AGENCY = "Q327333"
USA = "Q30"
ACCOUNT_PIDS = {"P2002": "X", "P2013": "Facebook", "P2003": "Instagram", "P2397": "YouTube channel",
                "P11245": "YouTube handle", "P4264": "LinkedIn", "P12361": "Bluesky", "P4033": "Mastodon",
                "P3789": "Telegram"}

LEXIPEDIA_DEFAULT = {
    "base_url": "https://lexipedia.wikibase.cloud",
    "sparql": "https://lexipedia.wikibase.cloud/query/sparql",
    "quickstatements": "https://lexipedia.wikibase.cloud/tools/quickstatements/#/v1=",
    "props": {"instance_of": "", "part_of": "", "wikidata_id": "", "source_key": "",
              "website": "", "phone": "", "email": "", "reference_url": "", "retrieved": ""},
    "classes": {"jurisdiction": "", "agency": ""},
}
REQUIRED_LEX = [("props", "instance_of"), ("props", "part_of"), ("props", "source_key"), ("props", "wikidata_id"),
                ("classes", "jurisdiction"), ("classes", "agency")]


def today():
    return time.strftime("+%Y-%m-%dT00:00:00Z/11", time.gmtime())


def q(s):
    """QuickStatements string literal."""
    return '"' + re.sub(r'[\t\n\r"]+', " ", str(s)).strip() + '"'


def phone(p):
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return f"+1-{d[:3]}-{d[3:6]}-{d[6:]}" if len(d) == 10 else ""


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", str(s).lower()).strip("-")[:60]


def qs_link(base, text, limit=60000):
    if not text or len(text) > limit:
        return None
    body = "||".join(ln.replace("\t", "|") for ln in text.splitlines() if ln.strip())
    return base + quote(body, safe="")


def _same_site(a, b):
    from urllib.parse import urlparse
    ha = (urlparse(a).hostname or "").lower().removeprefix("www.")
    hb = (urlparse(b).hostname or "").lower().removeprefix("www.")
    return bool(ha) and ha == hb


# ---------------------------------------------------------------------------
# jurisdiction level -> Wikidata
# ---------------------------------------------------------------------------
def wikidata_jurisdictions(conn, min_conf=0.6, states=(), include_accounts=True):
    sql = """SELECT j.jid, j.qid, j.display, j.wd_website, s.url, s.confidence, s.source
             FROM jurisdiction j JOIN site s USING(jid)
             WHERE j.qid IS NOT NULL AND s.status='found' AND (s.confidence>=? OR s.source='human')"""
    args = [min_conf]
    if states:
        sql += f" AND j.state IN ({','.join('?' * len(states))})"
        args += list(states)
    lines, review, n_sites, n_acc = [], [], 0, 0
    stamp = today()
    for r in conn.execute(sql + " ORDER BY j.state, j.display", args).fetchall():
        ref = f"S854\t{q(r['url'])}\tS813\t{stamp}"
        if not r["wd_website"]:
            lines.append(f"{r['qid']}\tP856\t{q(r['url'])}\t{ref}")
            n_sites += 1
        elif not _same_site(r["wd_website"], r["url"]):
            review.append({"jurisdiction": r["display"], "qid": r["qid"], "wikidata_has": r["wd_website"],
                           "scout_found": r["url"], "source": r["source"]})
        if include_accounts:
            for a in conn.execute("SELECT pid, value FROM account WHERE jid=? ORDER BY pid, value", (r["jid"],)):
                lines.append(f"{r['qid']}\t{a['pid']}\t{q(a['value'])}\t{ref}")
                n_acc += 1
    return {"text": "\n".join(lines), "statements": len(lines), "websites": n_sites, "accounts": n_acc,
            "review": review, "open_url": qs_link(WIKIDATA_QS, "\n".join(lines))}


# ---------------------------------------------------------------------------
# departments from Harvest org charts
# ---------------------------------------------------------------------------
def org_units(conn, states=()):
    """Flatten Harvest org charts into units with a stable key and their parent's key."""
    sql = """SELECT o.site, o.jid, o.data, j.qid, j.display, j.state, s.url AS home
             FROM orgchart o JOIN jurisdiction j ON j.jid=o.jid LEFT JOIN site s ON s.jid=o.jid"""
    args = []
    if states:
        sql += f" WHERE j.state IN ({','.join('?' * len(states))})"
        args += list(states)
    units = []
    for r in conn.execute(sql, args).fetchall():
        tree = json.loads(r["data"])
        root_key = f"jur:{r['jid']}"

        def walk(node, parent_key, path):
            for c in node.get("children", []):
                if c.get("kind") != "unit":
                    continue
                p = path + [slug(c["name"])]
                key = f"unit:{r['site']}|{'/'.join(p)}"
                units.append({"key": key, "parent": parent_key, "name": c["name"], "unit_kind": c.get("unit_kind") or "",
                              "contacts": c.get("contacts") or {}, "seen_by": c.get("seen_by") or [],
                              "jid": r["jid"], "jqid": r["qid"], "jurisdiction": r["display"],
                              "source": r["home"] or f"https://{r['site']}/"})
                walk(c, key, p)

        walk(tree, root_key, [])
    return units


def _xref(conn, target):
    return {r["key"]: r["qid"] for r in conn.execute("SELECT key, qid FROM xref WHERE target=?", (target,))}


def _desc(u):
    kind = u["unit_kind"] if u["unit_kind"] not in ("", "other") else "public agency"
    return f"{kind} of {u['jurisdiction'].replace(', ', ', ', 1)}"


def wikidata_departments(conn, pass_no=1, states=(), min_models=1):
    units = [u for u in org_units(conn, states) if u["jqid"] and len(u["seen_by"]) >= min_models]
    known = _xref(conn, "wikidata")
    stamp, lines = today(), []
    if pass_no == 1:
        for u in units:
            if u["key"] in known:
                continue
            ref = f"S854\t{q(u['source'])}\tS813\t{stamp}"
            lines += ["CREATE", f"LAST\tLen\t{q(u['name'])}", f"LAST\tDen\t{q(_desc(u))}",
                      f"LAST\tP31\t{GOVERNMENT_AGENCY}\t{ref}", f"LAST\tP17\t{USA}", f"LAST\tP131\t{u['jqid']}\t{ref}"]
            c = u["contacts"]
            if c.get("website", "").startswith("http"):
                lines.append(f"LAST\tP856\t{q(c['website'])}\t{ref}")
            if phone(c.get("phone")):
                lines.append(f"LAST\tP1329\t{q(phone(c['phone']))}\t{ref}")
            if "@" in c.get("email", ""):
                lines.append(f"LAST\tP968\t{q('mailto:' + c['email'].strip())}\t{ref}")
    else:
        # unit -> parent unit only: on Wikidata the city item is a place, not an organization,
        # so top-level units keep just "located in" (P131) from pass 1
        for u in units:
            child, parent = known.get(u["key"]), known.get(u["parent"])
            if child and parent and u["parent"].startswith("unit:"):
                lines.append(f"{child}\tP749\t{parent}\tS854\t{q(u['source'])}\tS813\t{stamp}")
    pending = sum(1 for u in units if u["key"] not in known)
    return {"text": "\n".join(lines), "units": len(units), "unresolved": pending,
            "open_url": qs_link(WIKIDATA_QS, "\n".join(lines))}


def resolve_wikidata(conn, user_agent, states=()):
    """Find Wikidata items for units: label (or '<Place> <label>') + located in (P131) the jurisdiction."""
    units = [u for u in org_units(conn, states) if u["jqid"]]
    by_j = {}
    for u in units:
        by_j.setdefault(u["jqid"], []).append(u)
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "application/sparql-results+json"})
    found = 0
    for jqid, us in by_j.items():
        place = us[0]["jurisdiction"].split(",")[0].replace("City of ", "").replace("Town of ", "")
        labels = {}
        for u in us:
            for lab in (u["name"], f"{place} {u['name']}"):
                labels.setdefault(lab.lower(), u)
        values = " ".join(q(lab) + "@en" for lab in {u["name"] for u in us} | {f"{place} {u['name']}" for u in us})
        sparql = f"SELECT ?item ?label WHERE {{ VALUES ?label {{ {values} }} ?item wdt:P131 wd:{jqid}; rdfs:label ?label . }}"
        r = s.post(WDQS, data={"query": sparql}, timeout=90)
        r.raise_for_status()
        for b in r.json()["results"]["bindings"]:
            u = labels.get(b["label"]["value"].lower())
            if u:
                conn.execute("INSERT OR REPLACE INTO xref VALUES('wikidata',?,?)",
                             (u["key"], b["item"]["value"].rsplit("/", 1)[-1]))
                found += 1
        conn.commit()
        time.sleep(1)
    return found


# ---------------------------------------------------------------------------
# Lexipedia (Wikibase Cloud): jurisdictions + departments, property IDs configurable
# ---------------------------------------------------------------------------
def lexipedia_config(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='lexipedia_config'").fetchone()
    cfg = json.loads(json.dumps(LEXIPEDIA_DEFAULT))
    if row:
        saved = json.loads(row["value"])
        for k, v in saved.items():
            if isinstance(v, dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save_lexipedia_config(conn, cfg):
    conn.execute("INSERT OR REPLACE INTO meta VALUES('lexipedia_config', ?)", (json.dumps(cfg),))
    conn.commit()


def lexipedia_missing(cfg):
    return [f"{a}.{b}" for a, b in REQUIRED_LEX if not cfg[a].get(b)]


def lexipedia(conn, cfg, pass_no=1, states=()):
    missing = lexipedia_missing(cfg)
    if missing:
        raise ValueError("Set these Lexipedia property/class IDs first: " + ", ".join(missing))
    P, C = cfg["props"], cfg["classes"]
    units = org_units(conn, states)
    known = _xref(conn, "lexipedia")
    stamp, lines = today(), []

    def ref(url):
        out = ""
        if P.get("reference_url"):
            out += f"\tS{P['reference_url'][1:]}\t{q(url)}"
        if P.get("retrieved"):
            out += f"\tS{P['retrieved'][1:]}\t{stamp}"
        return out

    jurs = {}
    for u in units:
        jurs.setdefault(u["jid"], u)
    if pass_no == 1:
        for jid, u in jurs.items():
            key = f"jur:{jid}"
            if key in known:
                continue
            lines += ["CREATE", f"LAST\tLen\t{q(u['jurisdiction'])}", f"LAST\tP{P['instance_of'][1:]}\t{C['jurisdiction']}",
                      f"LAST\tP{P['source_key'][1:]}\t{q(key)}"]
            if u["jqid"]:
                lines.append(f"LAST\tP{P['wikidata_id'][1:]}\t{q(u['jqid'])}")
        for u in units:
            if u["key"] in known:
                continue
            r = ref(u["source"])
            lines += ["CREATE", f"LAST\tLen\t{q(u['name'])}", f"LAST\tDen\t{q(_desc(u))}",
                      f"LAST\tP{P['instance_of'][1:]}\t{C['agency']}{r}", f"LAST\tP{P['source_key'][1:]}\t{q(u['key'])}"]
            c = u["contacts"]
            if P.get("website") and c.get("website", "").startswith("http"):
                lines.append(f"LAST\tP{P['website'][1:]}\t{q(c['website'])}{r}")
            if P.get("phone") and phone(c.get("phone")):
                lines.append(f"LAST\tP{P['phone'][1:]}\t{q(phone(c['phone']))}{r}")
            if P.get("email") and "@" in c.get("email", ""):
                lines.append(f"LAST\tP{P['email'][1:]}\t{q('mailto:' + c['email'].strip())}{r}")
    else:
        for u in units:
            child, parent = known.get(u["key"]), known.get(u["parent"])
            if child and parent:
                lines.append(f"{child}\tP{P['part_of'][1:]}\t{parent}{ref(u['source'])}")
    pending = sum(1 for u in units if u["key"] not in known) + sum(1 for j in jurs if f"jur:{j}" not in known)
    return {"text": "\n".join(lines), "units": len(units), "jurisdictions": len(jurs), "unresolved": pending,
            "open_url": qs_link(cfg["quickstatements"], "\n".join(lines))}


def resolve_lexipedia(conn, cfg, user_agent, batch=200):
    """Map source keys -> Lexipedia QIDs through its SPARQL endpoint."""
    P = cfg["props"]
    base = cfg["base_url"].rstrip("/")
    keys = [u["key"] for u in org_units(conn)] + sorted({f"jur:{u['jid']}" for u in org_units(conn)})
    s = requests.Session()
    s.headers.update({"User-Agent": user_agent, "Accept": "application/sparql-results+json"})
    found = 0
    for i in range(0, len(keys), batch):
        values = " ".join(q(k) for k in keys[i:i + batch])
        sparql = (f"SELECT ?item ?key WHERE {{ VALUES ?key {{ {values} }} "
                  f"?item <{base}/prop/direct/{P['source_key']}> ?key . }}")
        r = s.post(cfg["sparql"], data={"query": sparql}, timeout=90)
        r.raise_for_status()
        for b in r.json()["results"]["bindings"]:
            conn.execute("INSERT OR REPLACE INTO xref VALUES('lexipedia',?,?)",
                         (b["key"]["value"], b["item"]["value"].rsplit("/", 1)[-1]))
            found += 1
        conn.commit()
    return found


def content_hash(data):
    return hashlib.sha256(data.encode()).hexdigest()[:16]
