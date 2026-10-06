# govenator

Tools for building an open directory of every US local government: its official
website, online accounts, departments, and published contacts. Results go to
**Wikidata**, where [govdirectory.org](https://govdirectory.org) reads them, or to
**Lexipedia** first if you'd rather review them there.

| | What it does | Runs where |
|---|---|---|
| [`scout/`](scout/) | Finds the official site of every county and municipality (Census + Wikidata + .gov registry + domain checks), test-crawls it politely, rates its directory data on a county map, queues people for what it can't do, and exports QuickStatements | anywhere; can be a shared Streamlit app |
| [`harvest/`](harvest/) | A browser plugin plus a local daemon. Pages go in, and a local LLM (Ollama, Qwen) builds org charts, optionally checked by a second model | your machine |
| [`bridge/`](bridge/) | Carries pages from the shared Scout database to your local Harvest, and the org charts back | your machine |

```
               shared (Streamlit app + Turso database)              your Mac
  ┌─────────────────────────────────────────────────────┐   ┌──────────────────────────┐
  │ Scout app: map · Needs you · best pages · export    │   │ Harvest daemon + Ollama  │
  │   scout workers ── crawl ──▶ Turso ◀── people       │   │        ▲      │          │
  │                     harvest_queue ──────────────────┼──▶│ bridge ┘      ▼          │
  │                     orgchart ◀──────────────────────┼───│ bridge ◀── org charts    │
  └─────────────────────────────────────────────────────┘   └──────────────────────────┘
        └─▶ QuickStatements ─▶ Wikidata (govdirectory.org)  /  Lexipedia
```

## Run it on one machine

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r scout/requirements.txt -r harvest/dashboard/requirements.txt
python -m playwright install chromium          # optional: JavaScript-only sites
ollama pull qwen3.6                             # for Harvest
./scripts/start-local.sh                        # Harvest daemon + bridge + Scout app
```

Then, in the app's **Run** tab: **Set up the universe**, and scout one state with a
limit of about 50 to see real results. Everything lives in local SQLite files
(`scout/scout.db`, `harvest/harvest.db`).

## Share it (Turso + Streamlit Cloud + your Mac)

1. **Database.** Create a Turso database:
   ```bash
   turso db create scout
   turso db show scout --url        # -> SCOUT_DB
   turso db tokens create scout     # -> SCOUT_DB_TOKEN
   ```
   Setup is faster done locally and copied up:
   ```bash
   python3 scout/scout.py setup
   SCOUT_DEST_TOKEN=<token> python3 scout/scout.py migrate --to libsql://scout-you.turso.io
   ```
2. **App.** On [Streamlit Community Cloud](https://share.streamlit.io), deploy this
   repo with main file `scout/app.py`. Paste the contents of
   `.streamlit/secrets.toml.example`, filled in, under *Secrets*. Visitors can
   browse; people with `APP_PASSWORD` can scout, answer *Needs you*, and change
   settings.
3. **Your Mac.** Copy `.env.example` to `.env`, fill it in, and run
   `./scripts/start-workers.sh` (Harvest + Ollama + the bridge). For overnight
   crawling, run the scout here too: `SCOUT_DB=… SCOUT_DB_TOKEN=… python3
   scout/scout.py run --playwright`. A hosted app goes to sleep when idle, and a
   scout started from it stops then. Runs resume where they left off either way.

The Turso connection uses Turso's HTTP protocol with plain `requests`, so it needs
no compiled drivers on Streamlit Cloud.

## What gets exported

**Wikidata, jurisdiction level** (Export tab). Each government's official website
(P856, only where Wikidata has none) and the online accounts found on its
homepage or contact pages: X P2002, Facebook P2013, Instagram P2003, YouTube
P2397/P11245, LinkedIn P4264, Bluesky P12361, Mastodon P4033, Telegram P3789.
These are the properties the
[WikiProject Govdirectory](https://www.wikidata.org/wiki/Wikidata:WikiProject_Govdirectory)
asks for. Every statement carries a reference: reference URL (S854) plus
retrieved (S813). When Wikidata already lists a *different* website, the place
goes on a review list instead of being overwritten. Matches come from the
Census GNIS/FIPS codes, so each statement lands on the right item.

**Departments, from Harvest org charts.** Units become government agency items
(P31 Q327333) located in (P131) their jurisdiction, with their published phone
(P1329), email (P968), and website. Export is two passes:

1. **Find** units that already have items, so nothing is duplicated.
2. **Pass 1** creates the rest.
3. **Find** again to pick up the new IDs.
4. **Pass 2** links each unit to its parent unit (P749).

The same flow targets **Lexipedia**; you enter its property and class IDs once,
in the app. Job titles and the people holding them are never exported.

On notability: the WikiProject Govdirectory page says public agencies are
notable on Wikidata, so departments can go straight there. Lexipedia is the
fallback, or a staging area if you want to review first.

All exports are QuickStatements batches for you to review and run. Nothing is
written to Wikidata or Lexipedia directly.

## How the crawler behaves

It honours robots.txt (including `Crawl-delay`), sends one request per site
every 1.5 s by default, and identifies itself in its User-Agent (set
`SCOUT_CONTACT`). Sites behind a bot challenge are marked *blocked* and sent to
people (Needs you) or the Harvest plugin; it never uses stealth browsers or
solves CAPTCHAs. Search is optional and only through self-hosted SearXNG or the
Brave API, never by scraping Google or Bing.

## Tests

```bash
./scripts/test.sh
```

This runs unit tests; the manual-input, export, and bridge flows; a fake-world
crawl (five mock towns, Census and .gov fixtures, Playwright) against both a
local file and a Turso-compatible stand-in server; a headless run of the app;
and Harvest's tests. CI runs the same on every push
(`.github/workflows/tests.yml`).

## Status and honest limits

- **Tested here:** everything listed under Tests, plus the page scorer on real
  municipal HTML and .gov matching against the real CISA registry.
- **Not tested against the real services:**
  - **Turso:** the client follows Turso's HTTP protocol and passes against a
    stand-in server, but hasn't run against a real Turso database. The app's
    first page load is the real test.
  - **Census and Wikidata downloads:** the sandbox couldn't reach them. The 2026
    Census pipe-delimited format is handled, from your file.
  - **Lexipedia's SPARQL URIs and QuickStatements address:** these are standard
    Wikibase Cloud defaults and editable in the app.
  - **QuickStatements on Wikidata:** needs you logged in. Review each batch
    before running it.
- Scores measure how much contact-and-role structure a page has, not whether
  it's correct. Accuracy comes from Harvest's two-model comparison and your
  review.

Apache-2.0. See [LICENSE](LICENSE).
