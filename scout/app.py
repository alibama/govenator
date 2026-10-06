# Scout — find, test-index and rate every US county and municipality website.
#   streamlit run scout/app.py
# Local: uses scout/scout.db. Shared: set SCOUT_DB (libsql://…) and SCOUT_DB_TOKEN in
# .streamlit/secrets.toml or the environment, and optionally APP_PASSWORD for edits.
import json
import os
import subprocess
import sys
import time
from urllib.parse import quote, urlparse

import pandas as pd
import plotly.graph_objects as go   # not plotly.express: it imports xarray, which breaks some envs
import streamlit as st

import exports as E
import scout as S
import store

st.set_page_config(page_title="Scout — US local government websites", layout="wide")
HERE = os.path.dirname(os.path.abspath(__file__))


def secret(*names, default=""):
    for n in names:
        try:
            if n in st.secrets:
                return str(st.secrets[n])
        except Exception:
            pass
        if os.environ.get(n):
            return os.environ[n]
    return default


DB_URL = secret("SCOUT_DB", "TURSO_DATABASE_URL", default=S.DEFAULT_DB)
DB_TOKEN = secret("SCOUT_DB_TOKEN", "TURSO_AUTH_TOKEN")
APP_PASSWORD = secret("APP_PASSWORD")
REMOTE = store.is_remote(DB_URL)

with st.sidebar:
    st.title("Scout")
    if REMOTE:
        st.caption(f"Shared database: **{urlparse(DB_URL.replace('libsql://', 'https://')).hostname}**")
    else:
        DB_URL = st.text_input("Database", DB_URL, help="A local SQLite file. For a shared app, set SCOUT_DB to a "
                                                         "Turso libsql:// URL in secrets.")
    os.environ["SCOUT_DB"], os.environ["SCOUT_DB_TOKEN"] = DB_URL, DB_TOKEN   # for spawned workers
    who = st.text_input("Your name", st.session_state.get("who", ""), help="Recorded with anything you add.")
    st.session_state["who"] = who.strip()
    if APP_PASSWORD and not st.session_state.get("can_write"):
        pw = st.text_input("Editor password", type="password")
        if pw:
            st.session_state["can_write"] = pw == APP_PASSWORD
            if not st.session_state["can_write"]:
                st.error("Wrong password")
    can_write = (not APP_PASSWORD) or bool(st.session_state.get("can_write"))
    if not can_write:
        st.caption("Read-only. Enter the editor password to scout, add sites, or change settings.")
    st.caption("The scout reads public pages politely: robots.txt is honoured, one request per site every "
               "few seconds, and sites behind bot challenges are left for people and the Harvest plugin.")


@st.cache_resource
def _shared(url, token):
    return S.open_db(url, token)


def conn():
    return _shared(DB_URL, DB_TOKEN) if REMOTE else S.open_db(DB_URL, DB_TOKEN)


def q(sql, args=()):
    return store.df(conn(), sql, args)


def one(sql, args=()):
    r = conn().execute(sql, tuple(args)).fetchone()
    return r[0] if r else None


def spawn(args, logname):
    os.makedirs(S.DATA, exist_ok=True)
    log = open(os.path.join(S.DATA, logname), "a")
    return subprocess.Popen([sys.executable, os.path.join(HERE, "scout.py")] + args, cwd=HERE,
                            stdout=log, stderr=subprocess.STDOUT, start_new_session=True, env=dict(os.environ))


def latest_job():
    r = conn().execute("SELECT * FROM job ORDER BY id DESC LIMIT 1").fetchone()
    return dict(r) if r else None


@st.cache_data(show_spinner="Loading county boundaries…")
def counties_geojson():
    with open(S.ensure_geojson()) as f:
        return json.load(f)


def outline(tree):
    lines = []

    def walk(n, d):
        for c in n.get("children", []):
            bits = [f"**{c['name']}**" if c["kind"] == "unit" else c["name"]]
            ct = c.get("contacts") or {}
            bits += [v for v in (ct.get("phone"), ct.get("email"), ct.get("website")) if v]
            lines.append("  " * d + "- " + " · ".join(bits))
            walk(c, d + 1)

    walk(tree, 0)
    return "\n".join(lines)


# ---- live header ---------------------------------------------------------
@st.fragment(run_every="10s" if REMOTE else "5s")
def header():
    s = S.status(conn())
    total, stages, labels = s["jurisdictions"], s["by_stage"], s["by_label"]
    eligible = total - stages.get("skip", 0)
    scouted = eligible - stages.get("new", 0)
    usable = labels.get("good directory", 0) + labels.get("rich directory", 0)
    cols = st.columns(6)
    cols[0].metric("Governments", f"{eligible:,}")
    cols[1].metric("Scouted", f"{scouted:,}", f"{scouted / eligible:.0%}" if eligible else None, delta_color="off")
    cols[2].metric("Sites found", f"{stages.get('indexed', 0):,}")
    cols[3].metric("Good/rich directories", f"{usable:,}")
    cols[4].metric("Blocked", f"{stages.get('blocked', 0):,}")
    cols[5].metric("No site found", f"{stages.get('no_site', 0):,}")
    j = latest_job()
    if j and j["state"] == "running":
        alive = time.time() - (j["heartbeat"] or 0) < 180
        frac = (j["done"] / j["total"]) if j["total"] else 0
        rate = j["done"] / max(1, (j["heartbeat"] or time.time()) - j["started"])
        eta = (j["total"] - j["done"]) / rate / 3600 if rate > 0 else None
        st.progress(min(1.0, frac), text=f"{j['kind']} · {j['done']:,}/{j['total']:,}"
                    + (f" · ~{eta:.1f} h left" if eta and j["kind"] == "run" else "")
                    + f" · {j['current'] or ''}" + ("" if alive else " · ⚠ no heartbeat — the worker may have stopped"))


st.title("US local government websites")
if not one("SELECT COUNT(*) FROM jurisdiction"):
    st.info("No jurisdictions yet. Open **Run** and click **Set up the universe**. It loads every county and "
            "municipality from the Census, then matches the .gov registry and Wikidata.")
header()
all_states = sorted(q("SELECT DISTINCT state FROM jurisdiction WHERE stage<>'skip'")["state"].dropna())
tab_map, tab_needs, tab_j, tab_pages, tab_export, tab_run = st.tabs(
    ["Map", "Needs you", "Jurisdictions", "Best pages", "Export", "Run"])

# ==========================================================================
with tab_map:
    metric = st.radio("Colour counties by", ["Best score", "Average score", "Coverage", "Progress", "County government"],
                      horizontal=True,
                      help="Best: the strongest website among the county and its municipalities. Coverage: share of "
                           "governments with a good or rich directory. Progress: share already scouted.")
    c1, c2 = st.columns([3, 1])
    states = c1.multiselect("States", all_states)
    show_cities = c2.toggle("Show municipalities", value=False)
    where, args = "j.stage<>'skip' AND j.county_geoid IS NOT NULL", []
    if states:
        where += f" AND j.state IN ({','.join('?' * len(states))})"
        args += states
    agg = q(f"""SELECT j.county_geoid AS fips, COUNT(*) AS governments,
                SUM(j.stage<>'new') AS scouted, MAX(q.score) AS best, AVG(q.score) AS average,
                SUM(q.label IN ('good directory','rich directory')) AS usable,
                MAX(CASE WHEN j.kind='county' THEN q.score END) AS county_gov
                FROM jurisdiction j LEFT JOIN quality q USING(jid) WHERE {where} GROUP BY j.county_geoid""", args)
    if agg.empty:
        st.info("Nothing to map yet.")
    else:
        gj = counties_geojson()
        names = {f["id"]: f["properties"].get("NAME", "") for f in gj["features"]}
        for c_ in ("governments", "scouted", "best", "average", "usable", "county_gov"):
            agg[c_] = pd.to_numeric(agg[c_], errors="coerce")
        agg[["scouted", "usable"]] = agg[["scouted", "usable"]].fillna(0)
        agg["county"] = agg["fips"].map(names)
        agg["coverage"] = 100 * agg["usable"] / agg["scouted"].where(agg["scouted"] > 0)
        agg["progress"] = 100 * agg["scouted"] / agg["governments"].where(agg["governments"] > 0)
        col = {"Best score": "best", "Average score": "average", "Coverage": "coverage",
               "Progress": "progress", "County government": "county_gov"}[metric]
        if states:
            keep = set(agg["fips"])
            gj = {"type": "FeatureCollection", "features": [f for f in gj["features"] if f["id"] in keep]}
        hover = agg[["governments", "scouted", "best", "average", "usable"]].fillna(-1)
        fig = go.Figure(go.Choropleth(
            geojson=gj, locations=agg["fips"], z=agg[col], zmin=0, zmax=100,
            colorscale="Blues" if col == "progress" else "RdYlGn",
            marker_line_width=0.2, marker_line_color="#999", colorbar_title=metric,
            text=agg["county"], customdata=hover.values,
            hovertemplate="<b>%{text}</b><br>" + metric + ": %{z:.0f}<br>governments: %{customdata[0]:.0f}"
                          "<br>scouted: %{customdata[1]:.0f}<br>best: %{customdata[2]:.0f}"
                          "<br>average: %{customdata[3]:.0f}<br>good/rich: %{customdata[4]:.0f}<extra></extra>"))
        fig.update_geos(scope="usa")
        if show_cities:
            pts = q(f"""SELECT j.display, j.lat, j.lon, q.score, q.label FROM jurisdiction j JOIN quality q USING(jid)
                        WHERE j.kind<>'county' AND {where}""", args)
            if not pts.empty:
                fig.add_trace(go.Scattergeo(lat=pts["lat"], lon=pts["lon"], text=pts["display"] + " — " + pts["label"],
                                            mode="markers", hoverinfo="text", showlegend=False,
                                            marker=dict(size=4, color=pts["score"], colorscale="RdYlGn",
                                                        cmin=0, cmax=100, line=dict(width=0))))
        if states:
            fig.update_geos(fitbounds="locations", visible=False)
        fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), height=560)
        st.plotly_chart(fig, width="stretch")
        st.caption("Grey counties have nothing scouted yet. Connecticut and Rhode Island have no county governments, "
                   "so their counties show only their municipalities.")
        by_state = q(f"""SELECT j.state, COUNT(*) AS governments, SUM(j.stage<>'new') AS scouted,
                         SUM(j.stage='indexed') AS sites_found, SUM(q.label IN ('good directory','rich directory')) AS good_or_rich,
                         SUM(j.stage='blocked') AS blocked, ROUND(AVG(q.score),1) AS avg_score
                         FROM jurisdiction j LEFT JOIN quality q USING(jid) WHERE {where} GROUP BY j.state ORDER BY j.state""", args)
        st.dataframe(by_state, hide_index=True, width="stretch")

# ==========================================================================
with tab_needs:
    st.subheader("Needs you")
    st.caption("Places Scout couldn't finish on its own. Each takes about a minute: find the official site, "
               "say there isn't one, or paste the text of a page Scout can't read. Each one you open is held for "
               "you for 20 minutes so two people don't do the same one.")
    if not who:
        st.info("Enter your name in the sidebar to start.")
    elif not can_write:
        st.info("Enter the editor password in the sidebar to add information.")
    else:
        a, b = st.columns([2, 2])
        reasons = a.multiselect("Show me", list(S.NEEDS_YOU), default=["no site found", "blocked"])
        ny_states = b.multiselect("States", all_states, key="ny_states")
        counts = {r: one(f"SELECT COUNT(*) FROM jurisdiction j LEFT JOIN quality q USING(jid) WHERE {S.NEEDS_YOU[r]}")
                  for r in S.NEEDS_YOU}
        st.caption(" · ".join(f"{r}: {n:,}" for r, n in counts.items()))
        jid = st.session_state.get("ny_jid")
        if st.button("Next one", type="primary") or (not jid and reasons):
            jid = S.claim_next(conn(), who, reasons, ny_states)
            st.session_state["ny_jid"] = jid
        if not jid:
            st.success("Nothing waiting for these filters. Nice.")
        else:
            j = dict(conn().execute("SELECT * FROM jurisdiction WHERE jid=?", (jid,)).fetchone())
            site = conn().execute("SELECT * FROM site WHERE jid=?", (jid,)).fetchone()
            state_name = S.STATES.get(j["state"], j["state"])
            st.markdown(f"### {j['display']}")
            terms = f'"{j["base"]}" {state_name} {j["type"] or ""} official website'
            links = [f"[Search the web](https://duckduckgo.com/?q={quote(terms)})",
                     f"[Search Wikipedia](https://en.wikipedia.org/w/index.php?search={quote(j['base'] + ', ' + state_name)})"]
            if j["qid"]:
                links.append(f"[Wikidata {j['qid']}](https://www.wikidata.org/wiki/{j['qid']})")
            st.markdown(" · ".join(links))
            what = f"Scout's result: **{site['status'].replace('_', ' ')}**" if site else "Not scouted yet."
            if site and site["url"]:
                what += f" — {site['url']}"
            if site and site["note"]:
                what += f" ({site['note']})"
            st.write(what)
            tried = q("SELECT url, source, confidence FROM candidate WHERE jid=? ORDER BY confidence DESC", (jid,))
            if not tried.empty:
                with st.expander(f"Addresses Scout tried ({len(tried)})"):
                    st.dataframe(tried, hide_index=True, width="stretch")
            with st.form(f"needs_{jid}", clear_on_submit=True):
                outcome = st.radio("What did you find?", ["Found the official website", "No website exists",
                                                          "Only social media", "Skip this one"], horizontal=True)
                website = st.text_input("Official website", placeholder="https://www.example.gov")
                directory = st.text_input("Directory or staff page (optional)", placeholder="https://…/directory")
                accounts = st.text_area("Social media links (optional, one per line)", height=80)
                pasted = st.text_area("Blocked site? Paste the directory page's text here (optional)", height=120,
                                      help="Copy the visible text of the staff directory or elected officials page. "
                                           "It's saved as a page for this place and can go to Harvest.")
                note = st.text_input("Note (optional)")
                verify_now = st.checkbox("Check the website now", value=True)
                submitted = st.form_submit_button("Save and next")
            if submitted:
                code = {"Found the official website": "website", "No website exists": "none",
                        "Only social media": "social", "Skip this one": "skip"}[outcome]
                if code == "website" and not website.strip():
                    st.error("Add the website address, or pick another answer.")
                else:
                    S.save_manual(conn(), jid, who, code, website, directory, accounts, pasted, note)
                    if code in ("website", "social") and website.strip() and verify_now:
                        with st.spinner("Checking the site…"):
                            cfg = type("Cfg", (), dict(guess=False, search="", sitemap=True, pages=6))()
                            S.process(S.Net(delay=1.0), None, conn(), dict(conn().execute(
                                "SELECT * FROM jurisdiction WHERE jid=?", (jid,)).fetchone()), cfg)
                        r = conn().execute("SELECT label FROM quality WHERE jid=?", (jid,)).fetchone()
                        st.toast(f"{j['display']}: {r['label'] if r else 'saved'}")
                    else:
                        st.toast(f"Saved {j['display']}")
                    S.release(conn(), jid)
                    st.session_state["ny_jid"] = S.claim_next(conn(), who, reasons, ny_states)
                    st.rerun()

# ==========================================================================
with tab_j:
    a, b, c3, d = st.columns([1, 2, 1, 2])
    fs = a.selectbox("State", [""] + all_states, format_func=lambda x: x or "all")
    flabel = b.multiselect("Rating", ["rich directory", "good directory", "thin directory", "site, no directory",
                                      "no site found", "blocked", "no website (confirmed)", "social media only"])
    fkind = c3.selectbox("Kind", ["", "county", "place", "town"], format_func=lambda x: x or "all")
    ftext = d.text_input("Name contains")
    where, args = ["j.stage<>'skip'"], []
    if fs:
        where.append("j.state=?"); args.append(fs)
    if flabel:
        where.append(f"q.label IN ({','.join('?' * len(flabel))})"); args += flabel
    if fkind:
        where.append("j.kind=?"); args.append(fkind)
    if ftext:
        where.append("j.display LIKE ?"); args.append(f"%{ftext}%")
    df = q(f"""SELECT j.jid, j.display AS name, j.kind, j.state, j.stage, q.label AS rating, q.score,
               s.url AS website, s.platform, q.best_url AS best_page, q.best_score, j.qid
               FROM jurisdiction j LEFT JOIN quality q USING(jid) LEFT JOIN site s USING(jid)
               WHERE {' AND '.join(where)} ORDER BY q.score DESC NULLS LAST, j.display LIMIT 2000""", args)
    st.caption(f"{len(df):,} shown (max 2,000)")
    st.dataframe(df.drop(columns=["jid"]), hide_index=True, width="stretch",
                 column_config={"website": st.column_config.LinkColumn(), "best_page": st.column_config.LinkColumn(),
                                "score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f")})
    if not df.empty:
        jid = st.selectbox("Details for", df["jid"].tolist(), format_func=lambda x: df.loc[df.jid == x, "name"].iloc[0])
        j = dict(conn().execute("SELECT * FROM jurisdiction WHERE jid=?", (jid,)).fetchone())
        site = conn().execute("SELECT * FROM site WHERE jid=?", (jid,)).fetchone()
        qual = conn().execute("SELECT * FROM quality WHERE jid=?", (jid,)).fetchone()
        x, y = st.columns(2)
        with x:
            st.markdown(f"**{j['display']}** · {j['kind']} · stage `{j['stage']}`"
                        + (f" · [{j['qid']}](https://www.wikidata.org/wiki/{j['qid']})" if j["qid"] else " · not linked in Wikidata"))
            if site:
                st.write(f"Website: {site['url'] or '—'} ({site['status']}, via {site['source'] or '—'}"
                         + (f", {site['platform']}" if site["platform"] else "") + ")")
                if site["note"]:
                    st.warning(site["note"])
            if qual:
                st.write("Score parts:", json.loads(qual["parts"]))
            acc = q("SELECT pid, value, source FROM account WHERE jid=? ORDER BY pid", (jid,))
            if not acc.empty:
                acc.insert(0, "platform", acc["pid"].map(E.ACCOUNT_PIDS))
                st.write("Online accounts:")
                st.dataframe(acc, hide_index=True, width="stretch")
            hist = q("SELECT at, who, outcome, website, note FROM manual WHERE jid=? ORDER BY id DESC", (jid,))
            if not hist.empty:
                st.write("Added by people:")
                st.dataframe(hist, hide_index=True, width="stretch")
        with y:
            pg = q("""SELECT url, role, score, emails, phones, roles, depts, rows, via, anchor FROM page
                      WHERE jid=? ORDER BY score DESC""", (jid,))
            st.write("Pages tested:")
            st.dataframe(pg, hide_index=True, width="stretch", column_config={"url": st.column_config.LinkColumn()})
            best = q("SELECT url, title, text FROM page WHERE jid=? AND text IS NOT NULL ORDER BY score DESC", (jid,))
            if not best.empty:
                with st.expander("Best page text (what Harvest receives)"):
                    st.text(best["text"].iloc[0][:8000])
            oc = conn().execute("SELECT data, model, received_at FROM orgchart WHERE jid=?", (jid,)).fetchone()
            if oc:
                with st.expander(f"Org chart from Harvest ({oc['model']}, {oc['received_at']})"):
                    st.markdown(outline(json.loads(oc["data"])) or "_empty_")
        a, b = st.columns(2)
        if a.button("Re-scout this one now", disabled=not can_write):
            with st.spinner("Scouting…"):
                cfg = type("Cfg", (), dict(guess=True, search="", sitemap=True, pages=6))()
                S.process(S.Net(delay=1.0), None, conn(), j, cfg)
            st.rerun()
        if b.button("Queue best pages for Harvest", disabled=best.empty or not can_write):
            c = conn()
            c.executemany("INSERT OR IGNORE INTO harvest_queue(jid,url,title,text,who,queued_at) VALUES(?,?,?,?,?,?)",
                          [(jid, r["url"], r["title"], r["text"], who or "app", S.now_iso())
                           for r in best.to_dict("records")])
            c.commit()
            st.success(f"Queued {len(best)} page(s). The bridge on the Harvest machine picks them up.")

# ==========================================================================
with tab_pages:
    a, b, c3 = st.columns(3)
    min_score = a.slider("Minimum page score", 0, 100, 45)
    ps = b.multiselect("States ", all_states)
    topn = c3.number_input("Queue top N for Harvest", 1, 5000, 25)
    where, args = ["p.text IS NOT NULL", "p.score >= ?"], [min_score]
    if ps:
        where.append(f"j.state IN ({','.join('?' * len(ps))})"); args += ps
    best_pages = q(f"""SELECT p.jid, j.display AS jurisdiction, j.state, p.url, p.title, p.score, p.emails, p.phones,
                       p.roles, p.rows, p.via, p.text FROM page p JOIN jurisdiction j USING(jid)
                       WHERE {' AND '.join(where)} ORDER BY p.score DESC LIMIT 5000""", args)
    hq = {r[0]: r[1] for r in conn().execute("SELECT status, COUNT(*) FROM harvest_queue GROUP BY status").fetchall()}
    st.caption(f"{len(best_pages):,} pages scoring {min_score}+ (text kept for each site's top 3) · Harvest queue: "
               f"{hq.get('queued', 0)} waiting, {hq.get('sent', 0)} delivered · "
               f"{one('SELECT COUNT(*) FROM orgchart') or 0} org charts received")
    st.dataframe(best_pages.drop(columns=["jid", "text"]), hide_index=True, width="stretch",
                 column_config={"url": st.column_config.LinkColumn(),
                                "score": st.column_config.ProgressColumn(min_value=0, max_value=100, format="%.0f")})
    a, b = st.columns(2)
    a.download_button("Download CSV", best_pages.drop(columns=["text"]).to_csv(index=False),
                      file_name="best_pages.csv", mime="text/csv", disabled=best_pages.empty)
    if b.button(f"Queue top {int(topn)} for Harvest", disabled=best_pages.empty or not can_write):
        c = conn()
        rows = best_pages.head(int(topn)).to_dict("records")
        c.executemany("INSERT OR IGNORE INTO harvest_queue(jid,url,title,text,who,queued_at) VALUES(?,?,?,?,?,?)",
                      [(r["jid"], r["url"], r["title"], r["text"], who or "app", S.now_iso()) for r in rows])
        c.commit()
        st.success(f"Queued {len(rows)} page(s). The bridge delivers them to Harvest + Ollama and sends org charts back.")

# ==========================================================================
with tab_export:
    st.subheader("Wikidata: websites and online accounts")
    st.caption("This is what govdirectory.org shows: each government's official website and accounts, added to "
               "its Wikidata item with a reference (the page it was found on, and the date). Paste the batch into "
               "QuickStatements and review it there before running.")
    a, b = st.columns(2)
    min_conf = a.slider("Minimum confidence in the website match", 0.5, 1.0, 0.6, 0.05)
    ex_states = b.multiselect("States", all_states, key="ex_states")
    res = E.wikidata_jurisdictions(conn(), min_conf, ex_states)
    m1, m2, m3 = st.columns(3)
    m1.metric("Statements", res["statements"])
    m2.metric("Websites to add", res["websites"])
    m3.metric("Online accounts", res["accounts"])
    if res["text"]:
        st.code(res["text"][:4000] + ("\n…" if len(res["text"]) > 4000 else ""), language=None)
        x, y = st.columns(2)
        x.download_button("Download QuickStatements", res["text"], file_name="wikidata-jurisdictions.qs.txt")
        if res["open_url"]:
            y.link_button("Open in QuickStatements", res["open_url"])
    if res["review"]:
        with st.expander(f"Wikidata lists a different website for {len(res['review'])} — check these by hand"):
            st.dataframe(pd.DataFrame(res["review"]), hide_index=True, width="stretch")

    st.divider()
    st.subheader("Departments from Harvest org charts")
    n_oc = one("SELECT COUNT(*) FROM orgchart") or 0
    if not n_oc:
        st.info("No org charts yet. Queue best pages for Harvest; the bridge brings back org charts once Ollama "
                "has organized them.")
    else:
        st.caption(f"{n_oc} org chart(s). The WikiProject Govdirectory page says public agencies are notable on "
                   "Wikidata; Lexipedia is the place to stage them first if you'd rather review there. Units are "
                   "exported; job titles and the people holding them are not.")
        target = st.radio("Send departments to", ["Wikidata", "Lexipedia"], horizontal=True)
        min_models = st.slider("Only units found by at least this many models", 1, 3, 1)
        if target == "Wikidata":
            st.markdown("**1.** Find units that already have Wikidata items, so they aren't duplicated. "
                        "**2.** Run pass 1 in QuickStatements to create the rest. **3.** Find again to pick up "
                        "the new QIDs. **4.** Run pass 2 to link each unit to its parent.")
            if st.button("Find existing / new items on Wikidata", disabled=not can_write):
                with st.spinner("Querying Wikidata…"):
                    n = E.resolve_wikidata(conn(), S.UA)
                st.success(f"Matched {n} unit(s).")
            p1 = E.wikidata_departments(conn(), 1, min_models=min_models)
            p2 = E.wikidata_departments(conn(), 2, min_models=min_models)
            st.caption(f"{p1['units']} units · {p1['unresolved']} without a Wikidata item yet")
            x, y = st.columns(2)
            x.download_button("Pass 1 — create", p1["text"] or "", file_name="wikidata-departments-pass1.qs.txt",
                              disabled=not p1["text"])
            y.download_button("Pass 2 — link to parents", p2["text"] or "", file_name="wikidata-departments-pass2.qs.txt",
                              disabled=not p2["text"])
        else:
            cfg = E.lexipedia_config(conn())
            with st.expander("Lexipedia settings (property and class IDs on your Wikibase)",
                             expanded=bool(E.lexipedia_missing(cfg))):
                cfg["base_url"] = st.text_input("Wikibase URL", cfg["base_url"])
                cfg["sparql"] = st.text_input("SPARQL endpoint", cfg["sparql"])
                cfg["quickstatements"] = st.text_input("QuickStatements URL", cfg["quickstatements"])
                cols = st.columns(3)
                for i, k in enumerate(cfg["props"]):
                    cfg["props"][k] = cols[i % 3].text_input(f"Property: {k.replace('_', ' ')}", cfg["props"][k],
                                                             placeholder="P…", key=f"lp_{k}").strip().upper()
                for k in cfg["classes"]:
                    cfg["classes"][k] = st.text_input(f"Class item: {k}", cfg["classes"][k], placeholder="Q…",
                                                      key=f"lc_{k}").strip().upper()
                if st.button("Save Lexipedia settings", disabled=not can_write):
                    E.save_lexipedia_config(conn(), cfg)
                    st.success("Saved for everyone using this database.")
            missing = E.lexipedia_missing(cfg)
            if missing:
                st.warning("Set these first: " + ", ".join(missing))
            else:
                if st.button("Find items already on Lexipedia", disabled=not can_write):
                    with st.spinner("Querying Lexipedia…"):
                        n = E.resolve_lexipedia(conn(), cfg, S.UA)
                    st.success(f"Matched {n} item(s).")
                p1, p2 = E.lexipedia(conn(), cfg, 1), E.lexipedia(conn(), cfg, 2)
                st.caption(f"{p1['jurisdictions']} jurisdictions · {p1['units']} units · {p1['unresolved']} not on Lexipedia yet")
                x, y = st.columns(2)
                x.download_button("Pass 1 — create", p1["text"] or "", file_name="lexipedia-pass1.qs.txt",
                                  disabled=not p1["text"])
                y.download_button("Pass 2 — link to parents", p2["text"] or "", file_name="lexipedia-pass2.qs.txt",
                                  disabled=not p2["text"])

# ==========================================================================
with tab_run:
    if not can_write:
        st.info("Enter the editor password in the sidebar to set up or run the scout.")
    job = latest_job()
    running = job and job["state"] == "running" and time.time() - (job["heartbeat"] or 0) < 180

    st.subheader("1 · Set up the universe")
    n = one("SELECT COUNT(*) FROM jurisdiction") or 0
    st.caption(f"{n:,} jurisdictions loaded. Setup downloads the Census gazetteer (every county and incorporated "
               "place), matches the CISA .gov registry, and asks Wikidata for each one's official website and QID. "
               "It takes a few minutes, and re-running it is safe. On a shared database it's quicker to run setup "
               "locally and copy it up with `python3 scout.py migrate --to libsql://…`.")
    a, b, c3 = st.columns(3)
    with_towns = a.checkbox("Include towns and townships", help="New England and Midwest towns/townships are "
                                                                 "full local governments (~16,000 more).")
    use_wd = b.checkbox("Wikidata", value=True)
    use_gov = c3.checkbox(".gov registry", value=True)
    if st.button("Set up the universe", disabled=bool(running) or not can_write):
        args = ["setup"] + (["--with-towns"] if with_towns else []) + ([] if use_wd else ["--no-wikidata"]) \
               + ([] if use_gov else ["--no-dotgov"])
        spawn(args, "setup.log")
        st.success("Setup started. Progress appears at the top of the page.")

    st.subheader("2 · Scout")
    run_states = st.multiselect("States (empty = everything)", all_states)
    a, b, c3, d = st.columns(4)
    kinds = a.multiselect("Kinds", ["county", "place", "town"], default=["county", "place", "town"])
    workers = b.slider("Parallel sites", 1, 32, 12, help="Different sites at once. Each site still gets at most "
                                                         "one request every delay seconds.")
    delay = c3.number_input("Delay per site (s)", 0.5, 10.0, 1.5, 0.5)
    pages = d.slider("Pages tested per site", 2, 12, 6)
    a, b, c3 = st.columns(3)
    playwright = a.checkbox("Render JavaScript pages (Playwright)", value=not REMOTE,
                            help="Only for pages that are empty without JavaScript. Needs `playwright install chromium` "
                                 "on the machine running the scout.")
    refresh = b.checkbox("Re-scout ones already done")
    limit = c3.number_input("Limit (0 = all)", 0, 100000, 0, 50)
    with st.expander("Search API (optional, finds sites the registries miss)"):
        search = st.selectbox("Provider", ["", "searxng", "brave"],
                              format_func=lambda x: {"": "none", "searxng": "SearXNG (self-hosted)", "brave": "Brave Search API"}[x])
        searxng_url = st.text_input("SearXNG URL", secret("SEARXNG_URL", default="http://localhost:8080"))
        brave_key = st.text_input("Brave API key", secret("BRAVE_API_KEY"), type="password")
    todo_where = "stage<>'skip'" + ("" if refresh else " AND stage IN ('new','error')")
    targs = []
    if run_states:
        todo_where += f" AND state IN ({','.join('?' * len(run_states))})"
        targs += run_states
    if kinds:
        todo_where += f" AND kind IN ({','.join('?' * len(kinds))})"
        targs += kinds
    todo = one(f"SELECT COUNT(*) FROM jurisdiction WHERE {todo_where}", targs) or 0
    todo = min(todo, limit) if limit else todo
    est = todo * (pages + 3) * delay / max(1, workers) / 3600
    st.caption(f"{todo:,} to scout · rough time ≈ {est:.1f} h at these settings"
               + (" · on a hosted app the scout stops if the app goes to sleep; it resumes where it left off"
                  if REMOTE else ""))
    a, b = st.columns(2)
    if a.button("Start scouting", type="primary", disabled=bool(running) or not todo or not can_write):
        args = ["run", "--workers", str(workers), "--delay", str(delay), "--pages", str(pages)]
        args += ["--states", ",".join(run_states)] if run_states else []
        args += ["--kinds", ",".join(kinds)] if kinds else []
        args += ["--playwright"] if playwright else []
        args += ["--refresh"] if refresh else []
        args += ["--limit", str(limit)] if limit else []
        if search:
            args += ["--search", search, "--searxng-url", searxng_url]
            os.environ["BRAVE_API_KEY"] = brave_key
        spawn(args, "run.log")
        st.success("Scouting started. Watch the map fill in.")
    if b.button("Stop", disabled=not running or not can_write):
        c = conn()
        c.execute("UPDATE job SET stop=1 WHERE state='running'")
        c.commit()
        st.info("Stopping after the sites in flight finish.")
    with st.expander("Log"):
        for name in ("run.log", "setup.log"):
            p = os.path.join(S.DATA, name)
            if os.path.exists(p):
                st.caption(name)
                st.code("".join(open(p, errors="replace").readlines()[-25:]) or "(empty)")
