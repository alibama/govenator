"""
textify — turn a fetched HTML page into what Scout needs, with no model:

  * compact structured text (same conventions as the Harvest plugin's capture:
    "# heading", "- list item", "a | b | c" table rows, <email>/<tel:> targets,
    indented <select> options, decoded CivicPlus email obfuscation)
  * the page's links (for finding directory pages)
  * the site platform, if recognisable
  * directory-quality metrics and a 0-100 page score

Python stdlib only.
"""
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urldefrag

SKIP = {"style", "noscript", "svg", "iframe", "template", "canvas", "video", "audio",
        "picture", "button", "textarea", "object", "embed"}
BLOCK = {"p", "div", "section", "article", "aside", "header", "footer", "nav", "ul", "ol", "dl", "dt", "dd",
         "table", "thead", "tbody", "tfoot", "caption", "blockquote", "address", "figure", "figcaption",
         "main", "fieldset", "legend", "hr", "pre", "form", "body"}
VOID = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?<!\d)(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")
ROLE = re.compile(
    r"\b(mayor|vice[- ]mayor|council ?(?:wo)?m[ae]n|council ?members?|councilors?|alderm[ae]n|aldermen|"
    r"selectm[ae]n|select ?board|commissioners?|supervisors?|city manager|town manager|county administrator|"
    r"county executive|administrator|director|chief|clerk|treasurer|assessor|sheriff|attorney|"
    r"superintendent|coordinator|manager|registrar|chair(?:man|woman|person)?|secretary|engineer|"
    r"inspector|planner|marshal|recorder|auditor|comptroller)\b", re.I)
DEPT = re.compile(r"\b(departments?|division|office of|bureau|commission|board of|authority|committee)\b", re.I)

PLATFORMS = [
    ("CivicPlus", re.compile(r"civicplus|/Directory\.aspx|CivicAlerts\.aspx|civicengage", re.I)),
    ("Granicus", re.compile(r"granicus|govdelivery", re.I)),
    ("Revize", re.compile(r"revize", re.I)),
    ("CivicLive", re.compile(r"civiclive|schoolwires", re.I)),
    ("Vision", re.compile(r"visioninternet|vision cms", re.I)),
    ("OpenCities", re.compile(r"opencities", re.I)),
    ("Municode", re.compile(r"municode", re.I)),
    ("Tyler", re.compile(r"tylertech|tyler technologies", re.I)),
    ("WordPress", re.compile(r"wp-content|wp-includes", re.I)),
    ("Drupal", re.compile(r"drupal", re.I)),
    ("Squarespace", re.compile(r"squarespace", re.I)),
    ("Wix", re.compile(r"wix\.com|wixstatic", re.I)),
]


def clean(s):
    return re.sub(r"[\s\u00a0]+", " ", s or "").strip()


def decode_script(src):
    """CivicPlus hides emails in JS: var w="user"; var x="domain"  or  new Array("u","s",...)."""
    w = re.search(r'\bw\s*=\s*"([^"]*)"', src)
    x = re.search(r'\bx\s*=\s*"([^"]*)"', src)
    if w and x and w.group(1) and x.group(1):
        return f" <{w.group(1)}@{x.group(1)}>"
    a = re.search(r"new Array\(([^)]*)\)", src)
    if a:
        e = "".join(re.findall(r'"([^"]*)"', a.group(1)))
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", e):
            return f" <{e}>"
    return ""


class _Parser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.line, self.title = [], "", ""
        self.skip_tag, self.skip = None, 0
        self.in_title = False
        self.script = None
        self.row, self.cell, self.option = None, None, None
        self.href, self.anchor = None, None
        self.links, self.mailto, self.tel, self.rel_me = [], set(), set(), []
        self.n_rows = self.n_items = self.n_scripts = self.n_tables = 0

    # -- helpers --
    def push(self, t):
        t = t.rstrip()
        if t.strip():
            self.out.append(t)

    def flush(self):
        self.push(clean(self.line))
        self.line = ""

    def emit(self, s):
        if self.option is not None:
            self.option.append(s)
        elif self.cell is not None:
            self.cell.append(s)
        else:
            self.line += s
        if self.anchor is not None:
            self.anchor.append(s)

    # -- events --
    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip_tag:
                self.skip += 1
            return
        a = dict(attrs)
        if tag == "title":
            self.in_title = True
            return
        if tag == "script":
            self.n_scripts += 1
            self.script = []
            return
        if tag in SKIP:
            self.skip_tag, self.skip = tag, 1
            return
        if tag == "a":
            self.href = (a.get("href") or "").strip()
            self.rel = (a.get("rel") or "").lower()
            self.anchor = []
            if self.href.lower().startswith("mailto:"):
                self.mailto.add(self.href[7:].split("?")[0].strip().lower())
            elif self.href.lower().startswith("tel:"):
                self.tel.add(re.sub(r"\D", "", self.href)[-10:])
        elif re.fullmatch(r"h[1-6]", tag):
            self.flush()
            self.line = "#" * int(tag[1]) + " "
        elif tag == "li":
            self.flush()
            self.line = "- "
            self.n_items += 1
        elif tag == "tr":
            self.flush()
            self.row = []
            self.n_rows += 1
        elif tag in ("td", "th") and self.row is not None:
            self.cell = []
        elif tag == "option":
            self.option = []
        elif tag == "table":
            self.n_tables += 1
        elif tag == "br":
            if self.cell is not None:
                self.cell.append(" ")
            elif self.option is None:
                self.flush()
        elif tag in BLOCK and self.cell is None:
            self.flush()

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip_tag:
                self.skip -= 1
                if not self.skip:
                    self.skip_tag = None
            return
        if tag == "title":
            self.in_title = False
        elif tag == "script":
            if self.script is not None:
                dec = decode_script("".join(self.script))
                if dec:
                    self.emit(dec)
                    self.mailto.add(dec.strip(" <>").lower())
            self.script = None
        elif tag == "a":
            text = clean("".join(self.anchor or []))
            if self.href:
                low = self.href.lower()
                if low.startswith("mailto:"):
                    self.emit(f" <{self.href[7:].split('?')[0]}>")
                elif low.startswith("tel:"):
                    d = re.sub(r"\D", "", self.href)[-7:]
                    cur = "".join(self.cell) if self.cell is not None else self.line
                    if d and d not in re.sub(r"\D", "", cur):
                        self.emit(f" <tel:{self.href[4:]}>")
                elif not low.startswith(("javascript:", "#")):
                    self.links.append((self.href, text))
                    if "me" in getattr(self, "rel", "").split():
                        self.rel_me.append(self.href)
            self.href, self.anchor = None, None
        elif tag in ("td", "th") and self.row is not None and self.cell is not None:
            self.row.append(clean("".join(self.cell)))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            if self.cell is not None:
                self.row.append(clean("".join(self.cell)))
                self.cell = None
            self.push(" | ".join(c for c in self.row if c))
            self.row = None
        elif tag == "option" and self.option is not None:
            raw = "".join(self.option).rstrip()
            lead = len(raw) - len(raw.lstrip(" \u00a0\t"))
            t = clean(raw)
            if t:
                self.push("  " * min(8, lead // 2) + "- " + t)
            self.option = None
        elif re.fullmatch(r"h[1-6]", tag) or tag == "li" or (tag in BLOCK and self.cell is None):
            self.flush()

    def handle_data(self, data):
        if self.script is not None:
            self.script.append(data)
            return
        if self.skip:
            return
        if self.in_title:
            self.title += data
            return
        self.emit(data)


def analyze(html, url):
    """Parse one page. Returns dict(title, text, links[(abs_url, anchor)], platform, metrics, score)."""
    p = _Parser()
    try:
        p.feed(html or "")
        p.close()
    except Exception:
        pass
    p.flush()
    lines, prev = [], None
    for ln in p.out:
        if ln != prev:
            lines.append(ln)
        prev = ln
    body = "\n".join(lines)
    title = clean(p.title)

    links, seen = [], set()
    for href, text in p.links:
        try:
            u = urldefrag(urljoin(url, href))[0]
        except Exception:
            continue
        if u.startswith(("http://", "https://")) and u not in seen:
            seen.add(u)
            links.append((u, text[:120]))

    emails = {e.lower() for e in EMAIL.findall(body)} | {e for e in p.mailto if "@" in e}
    phones = {re.sub(r"\D", "", m)[-10:] for m in PHONE.findall(body)} | {t for t in p.tel if len(t) == 10}
    role_lines = sum(1 for ln in lines if ROLE.search(ln))
    dept_lines = sum(1 for ln in lines if DEPT.search(ln))
    head = (html or "")[:200000]
    platform = next((name for name, rx in PLATFORMS if rx.search(head)), "")
    m = {
        "emails": len(emails), "phones": len(phones), "roles": role_lines, "depts": dept_lines,
        "rows": p.n_rows, "items": p.n_items, "chars": len(body), "scripts": p.n_scripts,
    }
    m["js_shell"] = int(m["chars"] < 800 and (m["scripts"] >= 3 or re.search(r"enable javascript", body, re.I) is not None))
    text = f"# {title}\nURL: {url}\n\n{body}"
    return {"title": title, "text": text, "links": links, "platform": platform,
            "metrics": m, "score": page_score(m), "accounts": accounts(links, p.rel_me)}


def page_score(m):
    """0-100: how much harvestable directory data this page holds."""
    s = (min(30, 3 * m["emails"]) + min(20, 2 * m["phones"]) + min(25, 2.5 * m["roles"])
         + min(10, m["depts"]) + min(15, m["rows"] * 0.75))
    return round(min(100.0, s), 1)


# ---------------------------------------------------------------------------
# online accounts (what Govdirectory lists), as Wikidata property -> value
# ---------------------------------------------------------------------------
VENDORS = re.compile(r"civicplus|civicengage|granicus|govdelivery|revize|municode|tylertech|visioninternet|"
                     r"opencities|schoolwires|civiclive|nextdoor|wix|squarespace|wordpress|youtube$|"
                     r"facebook$|twitter$|instagram$|linkedin$|gov(tech)?$|egov", re.I)
_RESERVED = {"sharer", "sharer.php", "share", "dialog", "plugins", "intent", "home", "hashtag", "search", "i",
             "login", "signup", "explore", "p", "reel", "reels", "tv", "watch", "results", "groups", "events",
             "pages", "help", "policies", "privacy", "legal", "about", "tr", "embed", "feed"}


def accounts(links, rel_me=()):
    """[(pid, value, url)] for the government's own accounts linked from a page."""
    out, seen = [], set()

    def add(pid, value, url):
        value = value.strip().strip("/")
        if value and not VENDORS.search(value) and value.lower() not in _RESERVED and (pid, value.lower()) not in seen:
            seen.add((pid, value.lower()))
            out.append((pid, value, url))

    for url, _ in links:
        m = re.match(r"https?://(?:www\.|m\.|mobile\.)?([^/]+)(/[^?#]*)?(\?[^#]*)?", url, re.I)
        if not m:
            continue
        host, path, query = m.group(1).lower(), (m.group(2) or "/"), (m.group(3) or "")
        seg = [x for x in path.split("/") if x]
        if host in ("facebook.com", "fb.com") and seg:
            if seg[0] == "profile.php":
                pid = re.search(r"id=(\d+)", query)
                if pid:
                    add("P2013", pid.group(1), url)
            elif seg[0] not in _RESERVED:
                add("P2013", seg[0], url)
        elif host in ("twitter.com", "x.com") and seg and seg[0] not in _RESERVED:
            add("P2002", seg[0].lstrip("@"), url)
        elif host == "instagram.com" and seg and seg[0] not in _RESERVED:
            add("P2003", seg[0], url)
        elif host == "youtube.com" and seg:
            if seg[0] == "channel" and len(seg) > 1 and seg[1].startswith("UC"):
                add("P2397", seg[1], url)
            elif seg[0].startswith("@"):
                add("P11245", seg[0][1:], url)
        elif host == "linkedin.com" and len(seg) > 1 and seg[0] == "company":
            add("P4264", seg[1], url)
        elif host == "bsky.app" and len(seg) > 1 and seg[0] == "profile":
            add("P12361", seg[1], url)
        elif host in ("t.me", "telegram.me") and seg:
            add("P3789", seg[0], url)
    for url in rel_me:
        m = re.match(r"https?://([^/]+)/@([A-Za-z0-9_]+)/?$", url)
        if m and m.group(1).lower() not in ("youtube.com", "www.youtube.com", "threads.net", "www.threads.net"):
            add("P4033", f"{m.group(2)}@{m.group(1).lower()}", url)
    return out
