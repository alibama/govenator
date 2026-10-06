# Scout

Find the official website of every US county and municipality, test-index each
one, and rate how much harvestable directory data it publishes. You watch it all
fill in on a county choropleth. The best pages go to **Harvest**, where the local
LLM builds org charts. The one-click plugin is the backup for whatever Scout
can't reach.

```
Census gazetteer ── every county + incorporated place (+ optional towns/townships)
        │
        ├─ Wikidata  official website (P856) + QID, matched by GNIS (P590) / FIPS (P882)
        ├─ CISA .gov registry  matched by organization name or government domain pattern
        ├─ guessed domains (DNS-checked)  cityofX.gov, Xva.gov, co.X.va.us, …
        └─ search API (optional)  self-hosted SearXNG or Brave
        ▼
verify   the homepage must name the place (and the state, for guessed/searched hits)
crawl    robots.txt honoured · one request per site every few seconds · directory-like
         links, platform paths (CivicPlus /Directory.aspx), sitemap · Playwright only for
         pages that are empty without JavaScript
score    emails, phones, role titles, departments, staff tables → 0-100 per page and site
        ▼
scout.db ── map · jurisdiction browser · best pages ──▶ Harvest (LLM org charts)
```

## Run it

```bash
pip install -r requirements.txt
playwright install chromium          # optional, for JavaScript-only sites
streamlit run app.py                 # or ./start.sh
```

The database is a local `scout.db` by default. To share it, set `SCOUT_DB` to a
Turso `libsql://` URL and `SCOUT_DB_TOKEN` (environment or
`.streamlit/secrets.toml`); see the repository README for the full shared setup.

In the app, open **Run**:

1. **Set up the universe.** This takes a few minutes. It downloads the Census
   gazetteer, the .gov registry, and Wikidata matches. Tick *Include towns and
   townships* to cover New England and Midwest towns, which are the real local
   governments there (about 16,000 more jurisdictions).
2. **Scout.** Start with one state and a limit of about 50 to see real results,
   then let it run overnight. The map, progress bar, and ETA update live. **Stop**
   finishes the sites in flight, and starting again resumes where it left off.

The same steps from a terminal:

```bash
python3 scout.py setup [--with-towns]
python3 scout.py run --states VA --limit 50 --playwright
python3 scout.py run --playwright --workers 16        # everything, resumable
python3 scout.py status
```

Set `SCOUT_CONTACT="you@example.org"` so the crawler's User-Agent tells site
owners how to reach you.

## Files

`scout.py` pipeline + CLI · `textify.py` page converter, scorer, account finder ·
`geo.py` county lookup · `store.py` SQLite/Turso · `exports.py` QuickStatements ·
`app.py` Streamlit app

## What you see

- **Map**: counties coloured by *best* score (the strongest site among the county
  and its municipalities), *average*, *coverage* (share with a good or rich
  directory), *progress* (share scouted), or the *county government* alone. You
  can overlay municipalities as dots and filter to states.
- **Jurisdictions**: every government with its rating, website, platform, best
  page, and Wikidata QID. Each one shows the candidates tried, the pages tested
  with their metrics, and the best page's text. You can re-scout one on the spot.
- **Needs you**: one jurisdiction at a time for people to finish. Paste the
  official site (checked on the spot), mark *no website* or *social media only*,
  add account links, or paste the text of a page Scout can't read. Each item is
  held for 20 minutes for whoever opened it.
- **Best pages**: the database of best pages across the country, with CSV export
  and **Queue for Harvest** (the bridge delivers them to Harvest + Ollama).
- **Export**: QuickStatements for Wikidata (websites + online accounts, with
  references) and for departments from Harvest's org charts (Wikidata or
  Lexipedia, two passes).

Ratings: *rich directory* (best page ≥ 70) · *good* (≥ 45) · *thin* (≥ 20) ·
*site, no directory* · *no site found* · *blocked*.

The site score (0–100) adds up a verified website (25), the best page (45),
breadth from extra good pages (15), a known platform (5), and Wikidata (10 if
the item already lists this site, 5 if the item exists).

## How it behaves on other people's servers

- It honours robots.txt (RFC 9309: a 4xx means no rules; an unreachable file
  means don't crawl), including any `Crawl-delay`.
- It sends at most one request per host every `--delay` seconds (1.5 s by
  default). Parallelism is across *different* sites.
- It identifies itself honestly in the User-Agent. Sites that answer with a bot
  challenge are recorded as **blocked** and left alone; it doesn't use stealth
  browsers or solve CAPTCHAs. Open those in a browser and use the Harvest plugin.
- It doesn't scrape Google or Bing, which their terms forbid. Search is optional
  and only through a self-hosted SearXNG or the Brave Search API.

Rough timing: about 9 requests per site × 1.5 s each, across 12 sites at a
time, comes to roughly 6 hours for the ~22,600 counties and incorporated places,
and more with towns. The **Run** tab estimates time for your settings.

## Testing

```bash
bash tests/run_all.sh
```

- `test_scout.py`: names, guesses, link ranking, real-page scoring, Wikidata, robots rules
- `test_flows.py`: Needs-you answers and claims, QuickStatements exports, the bridge
- `e2e.sh` / `e2e_turso.sh`: the fake-world crawl against a local file and a Turso stand-in
- `app_smoke.py`: the app run headless

The fake world covers one case each: a good CivicPlus site with a robots-blocked
page, a guessed domain that belongs to a same-named town in another state, a
bot-challenged site, a JavaScript-only site, a county found through the .gov
registry, a census-designated place (not a government), and a Connecticut
planning region (no county government).

## Honest limits

- **Verified here:** the page converter and scorer on real Charlottesville pages,
  .gov matching against the real CISA registry (which found and fixed three
  matching bugs), the county lookup, the fake-world crawl including Playwright,
  the app headless, and the bridge to a real Harvest daemon.
- **Not verified here:** Census and Wikidata downloads and real municipal sites,
  because this sandbox can't reach them. The gazetteer loader tries this year's
  file and the previous few. If the Census URL has moved, download
  `*_Gaz_place_national.zip` and `*_Gaz_counties_national.zip` from the Census
  "Gazetteer Files" page into `data/` and run setup again.
- The county map uses Plotly's county boundaries, which predate Connecticut's
  2022 planning regions. Connecticut towns still map correctly, by location.
- Scores measure how much contact-and-role structure a page has, not whether
  it's correct. Accuracy is Harvest's job: two models, then your review.
- Many small towns have no website at all, or only a Facebook page. They'll stay
  *no site found*, and that's useful to know too.

Apache-2.0 (see the repository LICENSE).
