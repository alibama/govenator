#!/usr/bin/env python3
"""
scout — find the official website of every US county and municipality, test-index
it, and rate how much harvestable directory data it publishes.

  python3 scout.py setup                 # universe: Census gazetteer + .gov registry + Wikidata
  python3 scout.py run --states VA       # discover + crawl + score (resumable)
  python3 scout.py status

Pipeline per jurisdiction:
  1. candidates  Wikidata official website (by GNIS/FIPS) · CISA .gov registry ·
                 guessed domains (DNS-checked) · optional search API (SearXNG/Brave)
  2. verify      fetch the homepage; it must name the place (and the state, for
                 guessed/searched hits)
  3. crawl       robots.txt honoured, one request per host every --delay seconds;
                 follow likely directory links, platform paths, sitemap
  4. score       deterministic: emails, phones, role titles, departments, tables
Pages that need JavaScript are rendered with Playwright (optional). Pages behind a
bot challenge are marked "blocked" — no evasion; use the Harvest plugin for those.
"""
import argparse
import csv
import io
import json
import os
import queue
import random
import re
import socket
import sqlite3
import sys
import threading
import time
import traceback
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from urllib import robotparser
from urllib.parse import urlparse, urlunparse

import requests

import store
import textify

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
DEFAULT_DB = os.path.join(HERE, "scout.db")
COUNTIES_GEOJSON_URL = "https://raw.githubusercontent.com/plotly/datasets/master/geojson-counties-fips.json"
DOTGOV_URL = "https://raw.githubusercontent.com/cisagov/dotgov-data/main/current-full.csv"
GAZ_URL = "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/{y}_Gazetteer/{y}_Gaz_{kind}_national.zip"
WDQS = "https://query.wikidata.org/sparql"
UA_TOKEN = "GovdirectoryScout"
UA = f"{UA_TOKEN}/0.1 (civic-data research crawler; honours robots.txt{'; ' + os.environ['SCOUT_CONTACT'] if os.environ.get('SCOUT_CONTACT') else ''})"

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
# States where towns/townships (county subdivisions) are general-purpose governments.
TOWN_STATES = {"CT", "ME", "MA", "NH", "RI", "VT", "NJ", "NY", "PA", "MI", "MN", "WI", "OH", "IN", "IL",
               "KS", "MO", "NE", "ND", "SD"}

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS jurisdiction(
  jid TEXT PRIMARY KEY, kind TEXT, geoid TEXT, state TEXT, name TEXT, base TEXT, type TEXT,
  display TEXT, gnis TEXT, county_geoid TEXT, lat REAL, lon REAL, aland REAL,
  qid TEXT, wd_website TEXT, stage TEXT NOT NULL DEFAULT 'new', note TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS j_state ON jurisdiction(state);
CREATE INDEX IF NOT EXISTS j_stage ON jurisdiction(stage);
CREATE INDEX IF NOT EXISTS j_county ON jurisdiction(county_geoid);
CREATE TABLE IF NOT EXISTS candidate(
  jid TEXT, url TEXT, source TEXT, confidence REAL, PRIMARY KEY(jid, url));
CREATE TABLE IF NOT EXISTS site(
  jid TEXT PRIMARY KEY, url TEXT, source TEXT, status TEXT, http_status INTEGER, title TEXT,
  platform TEXT, confidence REAL, checked_at TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS page(
  jid TEXT, url TEXT, role TEXT, anchor TEXT, http_status INTEGER, score REAL,
  emails INTEGER, phones INTEGER, roles INTEGER, depts INTEGER, rows INTEGER, items INTEGER,
  chars INTEGER, js_shell INTEGER, via TEXT, title TEXT, text TEXT, fetched_at TEXT,
  PRIMARY KEY(jid, url));
CREATE TABLE IF NOT EXISTS quality(
  jid TEXT PRIMARY KEY, score REAL, label TEXT, best_url TEXT, best_score REAL,
  good_pages INTEGER, parts TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS dotgov(
  domain TEXT PRIMARY KEY, type TEXT, org TEXT, suborg TEXT, city TEXT, state TEXT);
CREATE TABLE IF NOT EXISTS job(
  id INTEGER PRIMARY KEY, kind TEXT, args TEXT, state TEXT, started REAL, heartbeat REAL,
  done INTEGER DEFAULT 0, total INTEGER DEFAULT 0, current TEXT, stop INTEGER DEFAULT 0,
  pid INTEGER, message TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS account(
  jid TEXT, pid TEXT, value TEXT, url TEXT, source TEXT, PRIMARY KEY(jid, pid, value));
CREATE TABLE IF NOT EXISTS manual(
  id INTEGER PRIMARY KEY, jid TEXT, who TEXT, outcome TEXT, website TEXT, directory_url TEXT,
  note TEXT, at TEXT);
CREATE INDEX IF NOT EXISTS manual_jid ON manual(jid);
CREATE TABLE IF NOT EXISTS claim(jid TEXT PRIMARY KEY, who TEXT, until REAL);
CREATE TABLE IF NOT EXISTS seed_page(jid TEXT, url TEXT, who TEXT, PRIMARY KEY(jid, url));
CREATE TABLE IF NOT EXISTS harvest_queue(
  id INTEGER PRIMARY KEY, jid TEXT, url TEXT UNIQUE, title TEXT, text TEXT, who TEXT,
  status TEXT NOT NULL DEFAULT 'queued', queued_at TEXT, sent_at TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS orgchart(
  site TEXT PRIMARY KEY, jid TEXT, model TEXT, data TEXT, data_hash TEXT, built_at TEXT, received_at TEXT);
CREATE TABLE IF NOT EXISTS xref(target TEXT, key TEXT, qid TEXT, PRIMARY KEY(target, key));
"""

WRITE = threading.Lock()   # serialize writes from worker threads


def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


_SCHEMA_DONE = set()


def db_url():
    return os.environ.get("SCOUT_DB") or DEFAULT_DB


def open_db(url=None, token=None):
    """Local SQLite file (default) or Turso (libsql://…, token in SCOUT_DB_TOKEN)."""
    url = url or db_url()
    token = token if token is not None else os.environ.get("SCOUT_DB_TOKEN")
    conn = store.connect(url, token)
    if url not in _SCHEMA_DONE:   # once per process: Turso round trips aren't free
        conn.executescript(SCHEMA)
        _SCHEMA_DONE.add(url)
    return conn


def norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s or "").lower().replace("&", " and ")).strip()


def log(msg):
    print(time.strftime("%H:%M:%S"), msg, flush=True)


# ---------------------------------------------------------------------------
# names
# ---------------------------------------------------------------------------
_SUFFIXES = [
    ("city and borough", "city and borough"), ("consolidated government (balance)", "city"),
    ("metropolitan government (balance)", "city"), ("unified government (balance)", "city"),
    ("metro government (balance)", "city"), ("urban county", "county"), ("(balance)", "city"),
    ("charter township", "township"), ("city and county", "city"), ("municipality", "municipality"),
    ("plantation", "plantation"), ("township", "township"), ("borough", "borough"), ("village", "village"),
    ("town", "town"), ("city", "city"), ("county", "county"), ("parish", "parish"),
    ("census area", "census area"), ("planning region", "planning region"), ("municipio", "municipio"),
]


def split_name(name):
    """'Charlottesville city' -> ('Charlottesville', 'city'); 'Albemarle County' -> ('Albemarle', 'county')."""
    n = name.strip()
    low = n.lower()
    for suf, typ in _SUFFIXES:
        if low.endswith(" " + suf):
            return n[: -len(suf)].strip(), typ
    return n, ""


def display_name(base, typ, kind, state):
    if kind == "county":
        return f"{base} {typ.title()}, {state}" if typ else f"{base}, {state}"
    return (f"{typ.title()} of {base}, {state}" if typ in ("city", "town", "village", "borough", "township")
            else f"{base}, {state}")


def no_government(kind, state, typ, name):
    """County-equivalents with no county government."""
    if kind != "county":
        return False
    return state in ("CT", "RI") or typ in ("census area", "planning region") or "planning region" in name.lower()


# ---------------------------------------------------------------------------
# HTTP: polite, robots-aware, test-mappable
# ---------------------------------------------------------------------------
BLOCK_MARKERS = re.compile(r"cf-chl|just a moment\.\.\.|attention required|captcha|are you a robot|"
                           r"access denied|request unsuccessful|incapsula|ddos protection", re.I)


class Resp:
    def __init__(self, url, status=0, final_url=None, text="", ctype="", error=None, blocked=False, disallowed=False):
        self.url, self.status, self.final_url, self.text = url, status, final_url or url, text
        self.ctype, self.error, self.blocked, self.disallowed = ctype, error, blocked, disallowed


class Net:
    def __init__(self, delay=1.5, timeout=20, max_bytes=3_000_000):
        self.delay, self.timeout, self.max_bytes = delay, timeout, max_bytes
        self.local = threading.local()
        self.gate = threading.Lock()
        self.next_ok = {}
        self.robots, self.robots_lock = {}, threading.Lock()
        # tests: {"www.example.gov": "http://127.0.0.1:9001"}
        self.host_map = json.loads(os.environ.get("SCOUT_TEST_HOSTS", "{}"))

    def session(self):
        if not hasattr(self.local, "s"):
            s = requests.Session()
            s.headers.update({"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.5",
                              "Accept-Language": "en-US,en;q=0.8"})
            self.local.s = s
        return self.local.s

    def rewrite(self, url):
        u = urlparse(url)
        if u.hostname in self.host_map:
            base = urlparse(self.host_map[u.hostname])
            return urlunparse((base.scheme, base.netloc, u.path or "/", "", u.query, "")), {"Host": u.hostname}
        return url, {}

    def resolves(self, host):
        if host in self.host_map:
            return True
        try:
            socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
            return True
        except Exception:
            return False

    def wait_turn(self, host, delay):
        while True:
            with self.gate:
                t = self.next_ok.get(host, 0)
                now = time.time()
                if now >= t:
                    self.next_ok[host] = now + delay
                    return
            time.sleep(min(0.5, t - now))

    def _raw(self, url, accept_xml=False):
        real, headers = self.rewrite(url)
        try:
            r = self.session().get(real, headers=headers, timeout=self.timeout, stream=True, allow_redirects=True)
            ctype = r.headers.get("content-type", "")
            body = b""
            if "html" in ctype or "xml" in ctype or "text" in ctype or not ctype:
                for chunk in r.iter_content(65536):
                    body += chunk
                    if len(body) > self.max_bytes:
                        break
            r.close()
            enc = r.encoding if r.encoding and r.encoding.lower() != "iso-8859-1" else None
            text = body.decode(enc or "utf-8", errors="replace")
            final = r.url
            if headers:   # map test URLs back to the logical host
                fu = urlparse(final)
                final = urlunparse((urlparse(url).scheme, headers["Host"], fu.path, "", fu.query, ""))
            blocked = r.status_code in (401, 403, 429, 503) and bool(BLOCK_MARKERS.search(text[:20000]))
            return Resp(url, r.status_code, final, text, ctype, None, blocked)
        except requests.RequestException as e:
            return Resp(url, 0, url, "", "", type(e).__name__)

    def robots_for(self, url):
        u = urlparse(url)
        key = f"{u.scheme}://{u.netloc}"
        with self.robots_lock:
            if key in self.robots:
                return self.robots[key]
        self.wait_turn(u.netloc, self.delay)
        r = self._raw(key + "/robots.txt")
        rp = robotparser.RobotFileParser()
        if r.status == 200 and "<html" not in r.text[:500].lower():
            rp.parse(r.text.splitlines())
        elif r.status == 0 or r.status >= 500:
            rp.parse(["User-agent: *", "Disallow: /"])   # RFC 9309: unreachable -> assume disallow
        else:
            rp.parse([])                                  # 4xx -> no rules
        with self.robots_lock:
            self.robots[key] = rp
        return rp

    def get(self, url):
        rp = self.robots_for(url)
        if not rp.can_fetch(UA_TOKEN, url):
            return Resp(url, 0, url, "", "", "robots.txt disallows", disallowed=True)
        host = urlparse(url).netloc
        cd = rp.crawl_delay(UA_TOKEN)
        self.wait_turn(host, max(self.delay, float(cd) if cd else 0))
        return self._raw(url)


class Renderer:
    """Headless Chromium on one dedicated thread (Playwright's sync API is single-threaded)."""

    def __init__(self, net):
        self.net, self.q = net, queue.Queue()
        self.ready, self.error = threading.Event(), None
        threading.Thread(target=self._run, daemon=True).start()
        self.ready.wait(60)
        if self.error:
            raise RuntimeError(self.error)

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            args = []
            if self.net.host_map:   # tests only: resolve fake hostnames to the mock server, keep the Host header
                rules = ", ".join(f"MAP {h} {urlparse(b).netloc}" for h, b in self.net.host_map.items())
                args.append(f"--host-resolver-rules={rules}")
            browser = pw.chromium.launch(args=args)
            ctx = browser.new_context(user_agent=UA)
        except Exception as e:
            self.error = f"Playwright unavailable: {e}"
            self.ready.set()
            return
        self.ready.set()
        while True:
            url, fut = self.q.get()
            try:
                u = urlparse(url)
                real = urlunparse(("http",) + tuple(u)[1:]) if u.hostname in self.net.host_map else url
                page = ctx.new_page()
                page.goto(real, wait_until="networkidle", timeout=30000)
                html = page.content()
                page.close()
                fut.set_result(html)
            except Exception as e:
                fut.set_exception(e)

    def render(self, url):
        fut = Future()
        self.q.put((url, fut))
        return fut.result(timeout=90)


# ---------------------------------------------------------------------------
# setup: universe + enrichment
# ---------------------------------------------------------------------------
def _download(url, path, timeout=180):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
    r.raise_for_status()
    with open(path, "wb") as f:
        f.write(r.content)


def ensure_geojson(path=None):
    path = path or os.path.join(DATA, "counties.geojson")
    if not os.path.exists(path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        log("downloading county boundaries …")
        _download(COUNTIES_GEOJSON_URL, path)
    return path


def gazetteer_path(kind, folder):
    """kind: place | counties | cousubs. Uses a local file if present, else downloads."""
    os.makedirs(folder, exist_ok=True)
    for fn in sorted(os.listdir(folder), reverse=True):
        if re.search(rf"_Gaz_{kind}_national\.(zip|txt)$", fn):
            return os.path.join(folder, fn)
    errors = []
    for y in (time.gmtime().tm_year, time.gmtime().tm_year - 1, 2025, 2024, 2023):
        url = GAZ_URL.format(y=y, kind=kind)
        dest = os.path.join(folder, f"{y}_Gaz_{kind}_national.zip")
        try:
            log(f"downloading {url}")
            _download(url, dest)
            return dest
        except Exception as e:
            errors.append(f"{y}: {e}")
            if os.path.exists(dest):
                os.remove(dest)
    raise RuntimeError(f"Could not download the Census {kind} gazetteer ({'; '.join(errors)}). "
                       f"Download *_Gaz_{kind}_national.zip from census.gov/geographies/reference-files "
                       f"(Gazetteer Files) into {folder}/ and run setup again.")


GAZ_REQUIRED = {"USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"}


def read_gazetteer(path):
    """Census gazetteer rows as dicts. Handles the classic tab-delimited files and the
    2026+ pipe-delimited ones, a UTF-8 BOM, and minor header differences."""
    if path.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            name = max((n for n in z.namelist() if n.lower().endswith((".txt", ".csv"))),
                       key=lambda n: z.getinfo(n).file_size)
            raw = z.read(name)
    else:
        raw = open(path, "rb").read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    first = text.split("\n", 1)[0]
    delim = max(("\t", "|", ","), key=first.count)
    rows = csv.DictReader(io.StringIO(text), delimiter=delim)
    alias = {"INTPTLON": "INTPTLONG", "NAMELSAD": "NAME"}
    header = {alias.get(h.strip().upper(), h.strip().upper()) for h in (rows.fieldnames or [])}
    missing = GAZ_REQUIRED - header
    if missing:
        raise ValueError(f"{os.path.basename(path)} doesn't look like a Census gazetteer file: missing "
                         f"{sorted(missing)}; header was: {first[:200]}")
    for r in rows:
        out = {}
        for k, v in r.items():
            if k:
                k = k.strip().upper()
                out[alias.get(k, k)] = (v or "").strip()
        yield out


def load_universe(conn, folder, geojson, with_towns=False):
    import geo
    counties = geo.Counties(geojson)
    recs = []
    for r in read_gazetteer(gazetteer_path("counties", folder)):
        base, typ = split_name(r["NAME"])
        recs.append(dict(jid="C" + r["GEOID"], kind="county", geoid=r["GEOID"], state=r["USPS"], name=r["NAME"],
                         base=base, type=typ or "county", gnis=r.get("ANSICODE", "").lstrip("0"),
                         county_geoid=r["GEOID"], lat=float(r["INTPTLAT"] or 0), lon=float(r["INTPTLONG"] or 0),
                         aland=float(r.get("ALAND") or 0)))
    place_keys = set()
    for r in read_gazetteer(gazetteer_path("place", folder)):
        if r.get("FUNCSTAT") not in ("A", "B"):
            continue  # skip CDPs (statistical) and inactive/consolidated
        base, typ = split_name(r["NAME"])
        lat, lon = float(r["INTPTLAT"] or 0), float(r["INTPTLONG"] or 0)
        cg = counties.locate(lon, lat, r["GEOID"][:2])
        place_keys.add((r["USPS"], norm(base), cg))
        recs.append(dict(jid="P" + r["GEOID"], kind="place", geoid=r["GEOID"], state=r["USPS"], name=r["NAME"],
                         base=base, type=typ, gnis=r.get("ANSICODE", "").lstrip("0"), county_geoid=cg,
                         lat=lat, lon=lon, aland=float(r.get("ALAND") or 0)))
    if with_towns:
        for r in read_gazetteer(gazetteer_path("cousubs", folder)):
            if r.get("FUNCSTAT") != "A" or r["USPS"] not in TOWN_STATES:
                continue
            base, typ = split_name(r["NAME"])
            if typ not in ("town", "township", "plantation"):
                continue
            cg = r["GEOID"][:5]
            if (r["USPS"], norm(base), cg) in place_keys:
                continue  # same government already listed as a place
            recs.append(dict(jid="T" + r["GEOID"], kind="town", geoid=r["GEOID"], state=r["USPS"], name=r["NAME"],
                             base=base, type=typ, gnis=r.get("ANSICODE", "").lstrip("0"), county_geoid=cg,
                             lat=float(r["INTPTLAT"] or 0), lon=float(r["INTPTLONG"] or 0),
                             aland=float(r.get("ALAND") or 0)))
    for d in recs:
        d["display"] = display_name(d["base"], d["type"], d["kind"], d["state"])
        skip = no_government(d["kind"], d["state"], d["type"], d["name"])
        d["stage"], d["note"] = ("skip", "no county government") if skip else ("new", None)
    with WRITE, conn:
        conn.executemany("""INSERT INTO jurisdiction(jid,kind,geoid,state,name,base,type,display,gnis,county_geoid,lat,lon,aland,stage,note)
                        VALUES(:jid,:kind,:geoid,:state,:name,:base,:type,:display,:gnis,:county_geoid,:lat,:lon,:aland,:stage,:note)
                        ON CONFLICT(jid) DO UPDATE SET name=excluded.name, base=excluded.base, type=excluded.type,
                          display=excluded.display, gnis=excluded.gnis, county_geoid=excluded.county_geoid,
                          lat=excluded.lat, lon=excluded.lon, aland=excluded.aland""", recs)
    return len(recs)


_ORG_STRIP = re.compile(r"^(the )?(city|town|village|borough|township|county|parish|municipality|"
                        r"incorporated village|charter township|consolidated government) of |"
                        r"\b(city|town|village|borough|township|county|parish|government|board of commissioners|"
                        r"board of supervisors|commission|fiscal court)\b", re.I)
_ORG_NOT_GOVT = re.compile(r"police|fire|library|court|marshal|sheriff|water|sewer|school|election|airport|"
                           r"utilit|transit|housing|museum|tourism|chamber|attorney|clerk of|treasurer|assessor|"
                           r"health|hospital|park district|port|emergency|911|e911|jail|probate|recorder", re.I)


def load_dotgov(conn, path=None):
    path = path or os.path.join(DATA, "dotgov.csv")
    if not os.path.exists(path):
        log("downloading the CISA .gov registry …")
        _download(DOTGOV_URL, path)
    rows = list(csv.DictReader(open(path, encoding="utf-8", errors="replace")))
    with WRITE, conn:
        conn.execute("DELETE FROM dotgov")
        conn.executemany("INSERT OR REPLACE INTO dotgov VALUES(?,?,?,?,?,?)",
                         [(r["Domain name"].lower(), r["Domain type"], r["Organization name"],
                           r.get("Suborganization name", ""), r.get("City", ""), r.get("State", "")) for r in rows])
    # match to jurisdictions
    by_state = {}
    for d in conn.execute("SELECT * FROM dotgov WHERE type IN ('City','County')"):
        by_state.setdefault(d["state"], []).append(d)
    def org_key(org, state):
        drop = set(norm(STATES.get(state, "")).split()) | {state.lower()}
        return " ".join(t for t in norm(_ORG_STRIP.sub(" ", org)).split() if t not in drop)

    cands = []
    for j in conn.execute("SELECT jid, kind, state, base, type FROM jurisdiction WHERE stage<>'skip'"):
        b = norm(j["base"])
        want = "County" if j["kind"] == "county" else "City"
        patterns = {h for h in guess_urls(dict(j)) if h.endswith(".gov")}
        for d in by_state.get(j["state"], []):
            if d["type"] != want:
                continue
            not_govt = _ORG_NOT_GOVT.search(" ".join((d["org"], d["suborg"] or "", d["domain"])))
            if org_key(d["org"], j["state"]) == b:
                conf = 0.3 if not_govt else 0.85
            elif d["domain"] in patterns:
                conf = 0.7   # registered .gov whose name fits the government's naming pattern
            elif j["kind"] != "county" and norm(d["city"]) == b and b in norm(d["org"]):
                conf = 0.2 if not_govt else 0.45   # e.g. "Springfield Township" vs "City of Springfield"
            else:
                continue
            cands.append((j["jid"], "https://" + d["domain"] + "/", "dotgov", conf))
    with WRITE, conn:
        conn.executemany("INSERT OR IGNORE INTO candidate VALUES(?,?,?,?)", cands)
    return len(rows), len(cands)


def load_wikidata(conn, batch=200, progress=None):
    """Official website (P856) + QID for every jurisdiction, matched by GNIS (P590) or county FIPS (P882)."""
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept": "application/sparql-results+json"})

    def run(prop, values):
        q = ("SELECT ?item ?code ?site WHERE { VALUES ?code { %s } ?item wdt:%s ?code . "
             "OPTIONAL { ?item wdt:P856 ?site } }" % (" ".join(f'"{v}"' for v in values), prop))
        for attempt in range(5):
            try:
                r = s.post(WDQS, data={"query": q}, timeout=90)
                if r.status_code == 200:
                    return r.json()["results"]["bindings"]
                if r.status_code in (429, 500, 502, 503, 504):
                    time.sleep(int(r.headers.get("retry-after", 0) or 0) or 5 * (attempt + 1))
                    continue
                r.raise_for_status()
            except requests.RequestException:
                time.sleep(5 * (attempt + 1))
        raise RuntimeError("Wikidata query failed repeatedly")

    groups = [("P590", [(j["jid"], j["gnis"]) for j in conn.execute(
                  "SELECT jid, gnis FROM jurisdiction WHERE kind<>'county' AND gnis<>'' AND stage<>'skip'")]),
              ("P882", [(j["jid"], j["geoid"]) for j in conn.execute(
                  "SELECT jid, geoid FROM jurisdiction WHERE kind='county' AND stage<>'skip'")])]
    total = sum(len(v) for _, v in groups)
    done = found = 0
    for prop, items in groups:
        for i in range(0, len(items), batch):
            chunk = items[i:i + batch]
            code_to_jid = {}
            for jid, code in chunk:
                code_to_jid.setdefault(code, []).append(jid)
            results = run(prop, list(code_to_jid))
            hits = {}
            for b in results:
                code = b["code"]["value"]
                qid = b["item"]["value"].rsplit("/", 1)[-1]
                site = b.get("site", {}).get("value")
                h = hits.setdefault(code, {"qid": qid, "sites": []})
                if site and site not in h["sites"]:
                    h["sites"].append(site)
            updates, cands = [], []
            for code, h in hits.items():
                site = sorted(h["sites"], key=lambda u: (not u.startswith("https"), len(u)))[0] if h["sites"] else None
                for jid in code_to_jid.get(code, []):
                    updates.append((h["qid"], site, jid))
                    if site:
                        found += 1
                        cands.append((jid, site, "wikidata", 0.9))
            with WRITE, conn:
                if updates:
                    conn.executemany("UPDATE jurisdiction SET qid=?, wd_website=? WHERE jid=?", updates)
                if cands:
                    conn.executemany("INSERT OR IGNORE INTO candidate VALUES(?,?,?,?)", cands)
            done += len(chunk)
            if progress:
                progress(done, total)
            time.sleep(1.0)
    return found


def setup(cfg):
    conn = open_db(cfg.db)
    geojson = ensure_geojson(cfg.geojson)
    jobid = job_start(conn, "setup", vars(cfg))
    try:
        job_progress(conn, jobid, 0, 3, "loading Census gazetteer")
        n = load_universe(conn, cfg.gazetteer_dir, geojson, cfg.with_towns)
        log(f"universe: {n} jurisdictions")
        if not cfg.no_dotgov:
            job_progress(conn, jobid, 1, 3, "matching the .gov registry")
            rows, c = load_dotgov(conn, cfg.dotgov_csv)
            log(f".gov registry: {rows} domains, {c} candidate matches")
        if not cfg.no_wikidata:
            job_progress(conn, jobid, 2, 3, "querying Wikidata")
            f = load_wikidata(conn, progress=lambda d, t: job_progress(conn, jobid, 2, 3, f"Wikidata {d}/{t}"))
            log(f"wikidata: {f} official websites")
        job_end(conn, jobid, "done", f"{n} jurisdictions")
    except Exception as e:
        job_end(conn, jobid, "error", str(e))
        raise


# ---------------------------------------------------------------------------
# discovery
# ---------------------------------------------------------------------------
def guess_urls(j):
    b = re.sub(r"[^a-z0-9]", "", j["base"].lower().replace("saint ", "st").replace("st. ", "st"))
    st, t = j["state"].lower(), (j["type"] or "").replace(" ", "")
    if not b:
        return []
    if j["kind"] == "county":
        t = t or "county"
        hosts = [f"{b}{t}.gov", f"{b}{t}{st}.gov", f"{b}co.gov", f"co.{b}.{st}.us", f"{b}{t}.org",
                 f"{b}{t}{st}.org", f"{b}{t}.com", f"{b}{t}.us"]
    else:
        hosts = [f"{b}.gov", f"{b}{st}.gov", f"{t}of{b}.gov" if t else None, f"{t}of{b}{st}.gov" if t else None,
                 f"ci.{b}.{st}.us", f"{b}.{st}.us", f"{t}of{b}.org" if t else None, f"{b}{st}.org",
                 f"{t}of{b}.com" if t else None, f"{b}{t}.org" if t else None]
    return [h for h in dict.fromkeys(hosts) if h]


def search_urls(j, cfg):
    q = f'"{j["base"]}" {j["type"] or ""} {STATES.get(j["state"], j["state"])} official website'
    urls = []
    try:
        if cfg.search == "searxng" and cfg.searxng_url:
            r = requests.get(cfg.searxng_url.rstrip("/") + "/search", params={"q": q, "format": "json"},
                             headers={"User-Agent": UA}, timeout=20)
            urls = [x.get("url") for x in r.json().get("results", [])]
        elif cfg.search == "brave" and cfg.brave_key:
            r = requests.get("https://api.search.brave.com/res/v1/web/search", params={"q": q, "count": 10},
                             headers={"X-Subscription-Token": cfg.brave_key, "Accept": "application/json"}, timeout=20)
            urls = [x.get("url") for x in r.json().get("web", {}).get("results", [])]
    except Exception as e:
        log(f"search failed for {j['display']}: {e}")
    junk = re.compile(r"wikipedia|facebook|twitter|x\.com|instagram|linkedin|youtube|yelp|tripadvisor|city-data|"
                      r"mapquest|zillow|realtor|niche\.com|usnews|bestplaces|ballotpedia|census\.gov|indeed", re.I)
    out = []
    for u in urls:
        if not u or junk.search(u):
            continue
        p = urlparse(u)
        out.append((f"{p.scheme}://{p.netloc}/", 0.5 if p.netloc.endswith((".gov", ".us")) else 0.35))
    return out[:5]


def _state_signal(j, title, text):
    name = STATES.get(j["state"], "").lower()
    hay = (title + " " + text[:20000]).lower()
    return (name and name in hay) or bool(re.search(rf"\b{j['state']}\s+\d{{5}}\b", title + " " + text[:20000]))


def verify(net, j, url, source, conf):
    r = net.get(url)
    if r.blocked:
        return {"status": "blocked", "url": url, "source": source, "http": r.status}
    if r.disallowed:
        return {"status": "robots", "url": url, "source": source, "http": 0}
    if r.status != 200 or not r.text:
        return None
    a = textify.analyze(r.text, r.final_url)
    title, text = norm(a["title"]), norm(a["text"][:20000])
    base = norm(j["base"])
    if not base:
        return None
    in_title, in_text = base in title, base in text[:6000]
    typ = norm(j["type"])
    phrase = typ and (f"{typ} of {base}" in title + " " + text or f"{base} {typ}" in title + " " + text)
    state = _state_signal(j, a["title"], a["text"])
    match = (0.5 if in_title else 0.25 if in_text else 0) + (0.25 if phrase else 0) + (0.25 if state else 0)
    final = round(0.4 * conf + 0.6 * match, 3)
    ok = (in_title or in_text) and final >= 0.5 and (state or source in ("wikidata", "dotgov"))
    if source == "human":
        ok, final = True, 1.0
    if not ok:
        return None
    return {"status": "found", "url": r.final_url, "source": source, "http": r.status, "confidence": final,
            "analysis": a, "html": r.text}


def discover(net, conn, j, cfg):
    cands = [dict(r) for r in conn.execute("SELECT url, source, confidence FROM candidate WHERE jid=? "
                                           "ORDER BY confidence DESC", (j["jid"],))]
    tried, fallback = set(), None

    def attempt(url, source, conf):
        nonlocal fallback
        key = urlparse(url).netloc.lower().removeprefix("www.")
        if key in tried:
            return None
        tried.add(key)
        v = verify(net, j, url, source, conf)
        if v and v["status"] != "found" and not fallback:
            fallback = v
        return v if v and v["status"] == "found" else None

    for c in cands:
        v = attempt(c["url"], c["source"], c["confidence"])
        if v:
            return v
    if cfg.guess:
        for host in guess_urls(j):
            if net.resolves(host) or (not host.startswith("www.") and net.resolves("www." + host)):
                v = attempt(f"https://{host}/", "guess", 0.4)
                if v:
                    return v
    if cfg.search:
        for url, conf in search_urls(j, cfg):
            v = attempt(url, "search", conf)
            if v:
                return v
    return fallback


# ---------------------------------------------------------------------------
# crawl + score
# ---------------------------------------------------------------------------
LINK_KW = [
    (re.compile(r"staff[\s_-]*directory|employee[\s_-]*directory|\bdirectory\b", re.I), 10),
    (re.compile(r"elected[\s_-]*officials|\bofficials\b|city[\s_-]*council|town[\s_-]*council|county[\s_-]*commission"
                r"|board[\s_-]*of[\s_-]*(supervisors|commissioners|aldermen|selectmen)|select[\s_-]*board|mayor", re.I), 8),
    (re.compile(r"\bdepartments?\b", re.I), 7),
    (re.compile(r"boards?[\s_-]*(and|&)?[\s_-]*commissions?|committees", re.I), 6),
    (re.compile(r"\bstaff\b|contact[\s_-]*us|\bcontacts?\b|who[\s_-]*to[\s_-]*call", re.I), 5),
    (re.compile(r"administration|city[\s_-]*manager|town[\s_-]*manager|county[\s_-]*administrator", re.I), 4),
    (re.compile(r"\bgovernment\b|\bleadership\b", re.I), 3),
]
LINK_BAD = re.compile(r"news|calendar|event|agenda|minutes|bid|rfp|pay|bill|login|sign[\s_-]*in|account|job|"
                      r"employment|career|facebook|twitter|youtube|instagram|linkedin|translate|privacy|accessib|"
                      r"\.(pdf|jpe?g|png|gif|docx?|xlsx?|zip|mp4)(\?|$)", re.I)
PLATFORM_PATHS = {"CivicPlus": ["/Directory.aspx"], "": ["/directory", "/staff-directory"]}


def same_site(url, home):
    h, b = urlparse(url).netloc.lower(), urlparse(home).netloc.lower().removeprefix("www.")
    return h == b or h.endswith("." + b) or b.endswith("." + h.removeprefix("www."))


def rank_links(links, home):
    scored = {}
    for url, text in links:
        if not same_site(url, home) or LINK_BAD.search(url) or LINK_BAD.search(text or ""):
            continue
        hay = (text or "") + " " + urlparse(url).path.replace("/", " ")
        s = sum(w for rx, w in LINK_KW if rx.search(hay))
        if s > 0:
            prev = scored.get(url)
            if not prev or s > prev[0]:
                scored[url] = (s, text)
    return sorted(((s, u, t) for u, (s, t) in scored.items()), reverse=True)


def sitemap_links(net, home, limit=4000):
    rp = net.robots_for(home)
    urls = list(rp.site_maps() or []) or [home.rstrip("/") + "/sitemap.xml"]
    found = []
    for sm in urls[:3]:
        r = net.get(sm)
        if r.status != 200:
            continue
        locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text)[:limit]
        found += [(u, "") for u in locs if not u.endswith(".xml")]
    return found


def index_site(net, renderer, j, found, cfg, seeds=()):
    home = found["url"]
    a = found["analysis"]
    pages = [{"url": home, "role": "home", "anchor": "", "http": found["http"], "a": a, "via": "http"}]
    if a["metrics"]["js_shell"] and renderer:
        try:
            a = textify.analyze(renderer.render(home), home)
            pages[0].update(a=a, via="browser")
        except Exception as e:
            log(f"render failed {home}: {e}")
    ranked = [(100, u, "(added by a person)") for u in seeds] + rank_links(a["links"], home)
    probes = PLATFORM_PATHS.get(a["platform"], []) + PLATFORM_PATHS[""]
    base = f"{urlparse(home).scheme}://{urlparse(home).netloc}"
    for p in probes:
        ranked.append((2, base + p, "(platform path)"))
    if cfg.sitemap and len(ranked) < cfg.pages:
        ranked += [(s, u, t) for s, u, t in rank_links(sitemap_links(net, home), home)]
    seen = {home.rstrip("/")}
    for _, url, anchor in ranked:
        if len(pages) > cfg.pages + len(seeds):
            break
        if url.rstrip("/") in seen:
            continue
        seen.add(url.rstrip("/"))
        r = net.get(url)
        if r.disallowed or r.blocked or r.status != 200 or not r.text:
            continue
        pa = textify.analyze(r.text, r.final_url)
        via = "http"
        if pa["metrics"]["js_shell"] and renderer:
            try:
                pa, via = textify.analyze(renderer.render(url), r.final_url), "browser"
            except Exception as e:
                log(f"render failed {url}: {e}")
        pages.append({"url": r.final_url, "role": "candidate", "anchor": anchor, "http": r.status, "a": pa, "via": via})
    return pages


def score_jurisdiction(conn, jid):
    j = conn.execute("SELECT * FROM jurisdiction WHERE jid=?", (jid,)).fetchone()
    site = conn.execute("SELECT * FROM site WHERE jid=?", (jid,)).fetchone()
    pages = conn.execute("SELECT url, score FROM page WHERE jid=? ORDER BY score DESC", (jid,)).fetchall()
    manual = conn.execute("SELECT outcome FROM manual WHERE jid=? ORDER BY id DESC LIMIT 1", (jid,)).fetchone()
    if j["stage"] == "skip":
        return None
    parts = {"website": 0, "directory": 0, "breadth": 0, "platform": 0, "wikidata": 0}
    best_url, best = (pages[0]["url"], pages[0]["score"]) if pages else (None, 0)
    good = sum(1 for p in pages if p["score"] >= 35)
    status = site["status"] if site else None
    if status == "found":
        parts["website"] = round(25 * min(1.0, (site["confidence"] or 0.6) / 0.8), 1)
        parts["platform"] = 5 if site["platform"] in ("CivicPlus", "Granicus", "Revize", "CivicLive", "Vision", "OpenCities") else 0
    elif status == "blocked":
        parts["website"] = 15   # it exists; Scout just can't read it
    if pages:                   # includes text a person pasted for a blocked site
        parts["directory"] = round(0.45 * best, 1)
        parts["breadth"] = 5 * min(3, good)
    if j["qid"]:
        parts["wikidata"] = 10 if (j["wd_website"] and site and site["url"] and
                                   same_site(site["url"], j["wd_website"])) else 5
    score = round(sum(parts.values()), 1)
    outcome = manual["outcome"] if manual else None
    if outcome == "none":
        label = "no website (confirmed)"
    elif outcome == "social" and not pages:
        label = "social media only"
    elif not pages and status == "blocked":
        label = "blocked"
    elif status != "found" and not pages:
        label = "no site found"
    elif best < 20:
        label = "site, no directory"
    elif best < 45:
        label = "thin directory"
    elif best < 70:
        label = "good directory"
    else:
        label = "rich directory"
    with WRITE, conn:
        conn.execute("INSERT OR REPLACE INTO quality VALUES(?,?,?,?,?,?,?,?)",
                     (jid, score, label, best_url, best, good, json.dumps(parts), now_iso()))
    return score


# ---------------------------------------------------------------------------
# human input ("Needs you" queue)
# ---------------------------------------------------------------------------
NEEDS_YOU = {
    "no site found": "j.stage='no_site' AND COALESCE(q.label,'') NOT IN ('no website (confirmed)','social media only')",
    "blocked": "j.stage='blocked' AND NOT EXISTS (SELECT 1 FROM page p WHERE p.jid=j.jid AND p.via='human')",
    "errors": "j.stage='error'",
    "site, no directory": "q.label='site, no directory'",
    "thin directory": "q.label='thin directory'",
}


def claim_next(conn, who, reasons, states=(), minutes=20):
    """Hand out one jurisdiction needing a person, skipping ones others are working on."""
    now = time.time()
    where = " OR ".join(f"({NEEDS_YOU[r]})" for r in reasons) or "0"
    sql = f"""SELECT j.jid FROM jurisdiction j LEFT JOIN quality q USING(jid)
              WHERE ({where}) AND NOT EXISTS (SELECT 1 FROM claim c WHERE c.jid=j.jid AND c.until>? AND c.who<>?)
              AND NOT EXISTS (SELECT 1 FROM manual m WHERE m.jid=j.jid AND m.outcome='skip' AND m.who=?)"""
    args = [now, who, who]
    if states:
        sql += f" AND j.state IN ({','.join('?' * len(states))})"
        args += list(states)
    sql += " ORDER BY (j.kind='county') DESC, j.aland DESC LIMIT 1"
    row = conn.execute(sql, args).fetchone()
    if not row:
        return None
    with WRITE, conn:
        conn.execute("INSERT OR REPLACE INTO claim VALUES(?,?,?)", (row["jid"], who, now + minutes * 60))
    return row["jid"]


def release(conn, jid):
    with WRITE, conn:
        conn.execute("DELETE FROM claim WHERE jid=?", (jid,))


def save_manual(conn, jid, who, outcome, website="", directory_url="", accounts_text="", pasted_text="", note=""):
    """Record what a person found. outcome: website | none | social | skip."""
    website = (website or "").strip()
    if website and not website.startswith(("http://", "https://")):
        website = "https://" + website
    with WRITE, conn:
        conn.execute("INSERT INTO manual(jid,who,outcome,website,directory_url,note,at) VALUES(?,?,?,?,?,?,?)",
                     (jid, who, outcome, website, directory_url, note, now_iso()))
        if website:
            conn.execute("INSERT OR REPLACE INTO candidate VALUES(?,?,?,?)", (jid, website, "human", 1.0))
        if directory_url:
            conn.execute("INSERT OR IGNORE INTO seed_page VALUES(?,?,?)", (jid, directory_url.strip(), who))
        links = [(u, "") for u in re.findall(r"https?://\S+", accounts_text or "")]
        for pid, value, url in textify.accounts(links, rel_me=[u for u, _ in links]):
            conn.execute("INSERT OR IGNORE INTO account VALUES(?,?,?,?,?)", (jid, pid, value, url, "human"))
        if pasted_text and pasted_text.strip():
            url = directory_url.strip() or website or f"manual://{jid}"
            import html as _html
            a = textify.analyze("<body>" + "".join(f"<p>{_html.escape(ln)}</p>" for ln in pasted_text.splitlines())
                                + "</body>", url)
            score, m = a["score"], a["metrics"]
            conn.execute("INSERT OR REPLACE INTO page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (jid, url, "candidate", "(pasted by a person)", 200, score, m["emails"], m["phones"],
                          m["roles"], m["depts"], m["rows"], m["items"], m["chars"], 0, "human",
                          "Pasted text", f"# Pasted text\nURL: {url}\n\n{pasted_text[:60000]}", now_iso()))
        if outcome == "none":
            conn.execute("UPDATE jurisdiction SET stage='no_site', note='confirmed: no website', updated_at=? WHERE jid=?",
                         (now_iso(), jid))
        elif outcome in ("website", "social") and website:
            conn.execute("UPDATE jurisdiction SET stage='new', updated_at=? WHERE jid=?", (now_iso(), jid))
        conn.execute("DELETE FROM claim WHERE jid=?", (jid,))
    score_jurisdiction(conn, jid)


def process(net, renderer, conn, j, cfg):
    jid = j["jid"]
    try:
        found = discover(net, conn, j, cfg)
        with WRITE, conn:
            conn.execute("DELETE FROM page WHERE jid=? AND COALESCE(via,'')<>'human'", (jid,))
            if not found:
                conn.execute("INSERT OR REPLACE INTO site(jid,status,checked_at) VALUES(?,?,?)", (jid, "not_found", now_iso()))
                conn.execute("UPDATE jurisdiction SET stage='no_site', updated_at=? WHERE jid=?", (now_iso(), jid))
            elif found["status"] != "found":
                conn.execute("INSERT OR REPLACE INTO site(jid,url,source,status,http_status,checked_at,note) VALUES(?,?,?,?,?,?,?)",
                             (jid, found["url"], found["source"], found["status"], found["http"], now_iso(),
                              "bot challenge — use the Harvest plugin" if found["status"] == "blocked" else "robots.txt disallows"))
                conn.execute("UPDATE jurisdiction SET stage=?, updated_at=? WHERE jid=?", (found["status"], now_iso(), jid))
        if found and found["status"] == "found":
            seeds = [r["url"] for r in conn.execute("SELECT url FROM seed_page WHERE jid=?", (jid,))]
            pages = index_site(net, renderer, j, found, cfg, seeds)
            keep = {p["url"] for p in sorted(pages, key=lambda p: -p["a"]["score"])[:3]}
            with WRITE, conn:
                conn.execute("INSERT OR REPLACE INTO site VALUES(?,?,?,?,?,?,?,?,?,?)",
                             (jid, found["url"], found["source"], "found", found["http"], found["analysis"]["title"][:300],
                              found["analysis"]["platform"], found["confidence"], now_iso(), None))
                for p in pages:
                    m = p["a"]["metrics"]
                    conn.execute("INSERT OR REPLACE INTO page VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                 (jid, p["url"], p["role"], (p["anchor"] or "")[:200], p["http"], p["a"]["score"],
                                  m["emails"], m["phones"], m["roles"], m["depts"], m["rows"], m["items"], m["chars"],
                                  m["js_shell"], p["via"], p["a"]["title"][:300],
                                  p["a"]["text"][:60000] if p["url"] in keep else None, now_iso()))
                conn.execute("UPDATE jurisdiction SET stage='indexed', note=NULL, updated_at=? WHERE jid=?", (now_iso(), jid))
                # online accounts linked from the homepage or contact-type pages belong to the government
                accts = list(pages[0]["a"].get("accounts", []))
                for p in pages[1:]:
                    if re.search(r"contact|connect|social|follow", (p["anchor"] or "") + " " + p["url"], re.I):
                        accts += p["a"].get("accounts", [])
                conn.execute("DELETE FROM account WHERE jid=? AND source='scout'", (jid,))
                for pid, value, url in accts:
                    conn.execute("INSERT OR IGNORE INTO account VALUES(?,?,?,?,?)", (jid, pid, value, url, "scout"))
        score_jurisdiction(conn, jid)
        return True
    except Exception as e:
        with WRITE, conn:
            conn.execute("UPDATE jurisdiction SET stage='error', note=?, updated_at=? WHERE jid=?",
                         (f"{type(e).__name__}: {e}"[:500], now_iso(), jid))
        log(f"error {j['display']}: {e}\n{traceback.format_exc(limit=3)}")
        return False


# ---------------------------------------------------------------------------
# jobs (the dashboard watches these)
# ---------------------------------------------------------------------------
def job_start(conn, kind, args):
    with WRITE, conn:
        cur = conn.execute("INSERT INTO job(kind,args,state,started,heartbeat,pid) VALUES(?,?,?,?,?,?)",
                           (kind, json.dumps(args, default=str), "running", time.time(), time.time(), os.getpid()))
        return cur.lastrowid


def job_progress(conn, jobid, done, total, current=None):
    with WRITE, conn:
        conn.execute("UPDATE job SET done=?, total=?, current=?, heartbeat=? WHERE id=?",
                     (done, total, current, time.time(), jobid))
        return conn.execute("SELECT stop FROM job WHERE id=?", (jobid,)).fetchone()["stop"]


def job_end(conn, jobid, state, message=None):
    with WRITE, conn:
        conn.execute("UPDATE job SET state=?, message=?, heartbeat=? WHERE id=?", (state, message, time.time(), jobid))


def run(cfg):
    conn = open_db(cfg.db)
    where, args = ["stage<>'skip'"], []
    if cfg.states:
        st = [s.strip().upper() for s in cfg.states.split(",") if s.strip()]
        where.append(f"state IN ({','.join('?' * len(st))})")
        args += st
    if cfg.kinds:
        ks = [k.strip() for k in cfg.kinds.split(",") if k.strip()]
        where.append(f"kind IN ({','.join('?' * len(ks))})")
        args += ks
    if not cfg.refresh:
        where.append("stage IN ('new','error')")
    todo = [dict(r) for r in conn.execute(f"SELECT * FROM jurisdiction WHERE {' AND '.join(where)}", args)]
    random.Random(7).shuffle(todo)   # spread work so the map fills in evenly
    if cfg.limit:
        todo = todo[: cfg.limit]
    jobid = job_start(conn, "run", vars(cfg))
    log(f"scouting {len(todo)} jurisdictions with {cfg.workers} workers")
    net = Net(delay=cfg.delay, timeout=cfg.timeout)
    renderer = None
    if cfg.playwright:
        try:
            renderer = Renderer(net)
        except Exception as e:
            log(f"{e} — continuing without browser rendering")
    done, stopped = 0, False
    job_progress(conn, jobid, 0, len(todo))
    with ThreadPoolExecutor(max_workers=cfg.workers) as ex:
        futs = {ex.submit(process, net, renderer, conn, j, cfg): j for j in todo}
        for fut in as_completed(futs):
            done += 1
            j = futs[fut]
            if job_progress(conn, jobid, done, len(todo), j["display"]):
                stopped = True
                log("stop requested — finishing in-flight jurisdictions")
                for f in futs:
                    f.cancel()
                break
    job_end(conn, jobid, "stopped" if stopped else "done", f"{done}/{len(todo)}")
    log(f"finished: {done}/{len(todo)}")


TABLES = ["jurisdiction", "candidate", "site", "page", "quality", "dotgov", "account", "manual", "claim",
          "seed_page", "harvest_queue", "orgchart", "xref", "meta"]


def migrate(src, dest_url, dest_token=None):
    dest = open_db(dest_url, dest_token)
    for t in TABLES:
        cur = src.execute(f"SELECT * FROM {t}")
        cols = [d[0] for d in cur.description]
        rows = [tuple(r) for r in cur.fetchall()]
        if rows:
            dest.executemany(f"INSERT OR REPLACE INTO {t}({','.join(cols)}) VALUES({','.join('?' * len(cols))})", rows)
            dest.commit()
        log(f"{t}: {len(rows)} rows")


def status(conn):
    out = {"jurisdictions": conn.execute("SELECT COUNT(*) FROM jurisdiction").fetchone()[0]}
    out["by_stage"] = {r[0]: r[1] for r in conn.execute("SELECT stage, COUNT(*) FROM jurisdiction GROUP BY stage")}
    out["by_label"] = {r[0]: r[1] for r in conn.execute("SELECT label, COUNT(*) FROM quality GROUP BY label")}
    out["with_candidates"] = conn.execute("SELECT COUNT(DISTINCT jid) FROM candidate").fetchone()[0]
    out["with_qid"] = conn.execute("SELECT COUNT(*) FROM jurisdiction WHERE qid IS NOT NULL").fetchone()[0]
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=None, help="SQLite path or libsql:// URL (default: $SCOUT_DB or scout.db; "
                                                "Turso token from $SCOUT_DB_TOKEN)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("setup", help="load every county + municipality, then .gov registry and Wikidata")
    s.add_argument("--gazetteer-dir", default=DATA)
    s.add_argument("--geojson", default=None)
    s.add_argument("--dotgov-csv", default=None)
    s.add_argument("--with-towns", action="store_true", help="also New England/Midwest towns and townships")
    s.add_argument("--no-wikidata", action="store_true")
    s.add_argument("--no-dotgov", action="store_true")

    r = sub.add_parser("run", help="discover, crawl and score (resumable)")
    r.add_argument("--states", default="", help="e.g. VA,NC (default: all)")
    r.add_argument("--kinds", default="", help="county,place,town (default: all)")
    r.add_argument("--workers", type=int, default=12)
    r.add_argument("--limit", type=int, default=0)
    r.add_argument("--pages", type=int, default=6, help="pages to test per site besides the homepage")
    r.add_argument("--delay", type=float, default=1.5, help="seconds between requests to the same host")
    r.add_argument("--timeout", type=int, default=20)
    r.add_argument("--refresh", action="store_true", help="re-scout jurisdictions already done")
    r.add_argument("--playwright", action="store_true", help="render JavaScript-only pages with headless Chromium")
    r.add_argument("--no-guess", dest="guess", action="store_false")
    r.add_argument("--no-sitemap", dest="sitemap", action="store_false")
    r.add_argument("--search", choices=["", "searxng", "brave"], default=os.environ.get("SCOUT_SEARCH", ""))
    r.add_argument("--searxng-url", default=os.environ.get("SEARXNG_URL", ""))
    r.add_argument("--brave-key", default=os.environ.get("BRAVE_API_KEY", ""))

    m = sub.add_parser("migrate", help="copy this database to another (e.g. local file -> Turso)")
    m.add_argument("--to", required=True, help="destination URL, e.g. libsql://scout-you.turso.io")
    m.add_argument("--to-token", default=os.environ.get("SCOUT_DEST_TOKEN"))

    sub.add_parser("rescore", help="recompute quality scores")
    sub.add_parser("status")
    cfg = ap.parse_args(argv)

    if cfg.cmd == "setup":
        setup(cfg)
    elif cfg.cmd == "run":
        run(cfg)
    elif cfg.cmd == "migrate":
        migrate(open_db(cfg.db), cfg.to, cfg.to_token)
    elif cfg.cmd == "rescore":
        conn = open_db(cfg.db)
        for (jid,) in conn.execute("SELECT jid FROM jurisdiction WHERE stage NOT IN ('new','skip')").fetchall():
            score_jurisdiction(conn, jid)
    elif cfg.cmd == "status":
        print(json.dumps(status(open_db(cfg.db)), indent=2))


if __name__ == "__main__":
    main()
