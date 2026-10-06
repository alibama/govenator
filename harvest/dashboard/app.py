# Harvest dashboard — reads the daemon's SQLite. Nothing to configure.
#   streamlit run dashboard/app.py
import json
import os
import sys

import pandas as pd
import streamlit as st

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "daemon"))
import harvestd as H  # noqa: E402

st.set_page_config(page_title="Harvest", layout="wide")
db_path = st.sidebar.text_input("Harvest DB", os.environ.get("HARVEST_DB", H.DEFAULT_DB))
if not os.path.exists(db_path):
    st.title("Harvest")
    st.info("No harvest database yet. Start the daemon, then index a page with the browser plugin:\n\n"
            "`python3 daemon/harvestd.py --models qwen3.6`")
    st.stop()


def conn():
    return H.open_db(db_path)


def table(sql, args=()):
    c = conn()
    try:
        return pd.read_sql_query(sql, c, params=args)
    finally:
        c.close()


# ---- live status (refreshes itself) --------------------------------------
@st.fragment(run_every="5s")
def live():
    c = conn()
    s = H.status(c)
    c.close()
    p = s["pages"]
    cols = st.columns(5)
    cols[0].metric("Worker", ("● " if s["worker"]["alive"] else "○ ") + str(s["worker"]["status"]))
    cols[1].metric("Queued", p.get("pending", 0) + p.get("parsing", 0))
    cols[2].metric("Parsed", p.get("done", 0))
    cols[3].metric("Errors", p.get("error", 0))
    cols[4].metric("Sites", s["sites"])
    st.caption(f"Models: {', '.join(s['models']) or '—'}"
               + (f"  ·  now: {s['worker']['current']}" if s["worker"]["current"] else ""))
    if not s["worker"]["alive"]:
        st.warning("The worker isn't running. Start it with `python3 daemon/harvestd.py`.")


st.title("Harvest")
live()
models = H.status(conn())["models"]
tab_org, tab_pages, tab_cmp = st.tabs(["Org charts", "Pages", "Compare models"])

# ---- org charts ----------------------------------------------------------
with tab_org:
    sites = table("""SELECT s.site, COALESCE(s.name, s.site) AS name, s.organized_at, s.dirty,
                     (SELECT COUNT(*) FROM page p WHERE p.site = s.site) AS pages
                     FROM site s ORDER BY COALESCE(s.organized_at, '') DESC""")
    if sites.empty:
        st.info("Nothing indexed yet. Click **Index this page** in the browser plugin on a government page.")
    else:
        labels = {r.site: f"{r.name} — {r.pages} page(s)" for r in sites.itertuples()}
        site = st.selectbox("Jurisdiction", list(labels), format_func=labels.get)
        c = conn()
        row = c.execute("SELECT model, data, created_at FROM site_model WHERE site=? ORDER BY id DESC LIMIT 1",
                        (site,)).fetchone()
        c.close()
        if not row:
            st.info("Not organized yet. It builds automatically shortly after the last page from this site is parsed.")
        else:
            tree = json.loads(row["data"])
            stt = tree.get("stats", {})
            st.caption(f"{stt.get('units', 0)} units ({stt.get('nested_units', 0)} nested, {stt.get('merged', 0)} merged) · "
                       f"{stt.get('positions', 0)} positions · organized by {stt.get('method', '?')} · {row['created_at']}")
            if len(models) > 1:
                st.caption("Dashed boxes were found by only some of the models; check those first.")
            st.graphviz_chart(H.tree_to_dot(tree, n_models=len(models)), width="stretch")
            with st.expander("Outline"):
                st.markdown(H.tree_to_outline(tree) or "_empty_")
            st.download_button("Download org chart (JSON)", json.dumps(tree, indent=2),
                               file_name=f"{site}-orgchart.json", mime="application/json")
        if st.button("Re-organize now"):
            c = conn()
            with c:
                c.execute("UPDATE site SET dirty=1, dirty_at=0 WHERE site=?", (site,))
            c.close()
            st.success("Queued. The worker will rebuild it within a few seconds.")

# ---- pages ---------------------------------------------------------------
with tab_pages:
    pages = table("SELECT id, site, title, status, captured_at, url, error FROM page ORDER BY id DESC")
    if pages.empty:
        st.info("No pages yet.")
    else:
        st.dataframe(pages.drop(columns=["error"]), width="stretch", hide_index=True)
        pid = st.selectbox("Inspect a page", pages["id"].tolist(),
                           format_func=lambda i: f"#{i} {pages.loc[pages.id == i, 'title'].iloc[0][:70]}")
        c = conn()
        pg = c.execute("SELECT * FROM page WHERE id=?", (pid,)).fetchone()
        ex = c.execute("SELECT model, data, ok, error, ms FROM extraction WHERE page_id=? AND text_hash=?",
                       (pid, pg["text_hash"])).fetchall()
        c.close()
        flags = json.loads(pg["flags"] or "[]")
        if flags:
            st.warning("This page contains lines that look like instructions to an AI. They were treated as data, "
                       "but review this page's results:\n\n" + "\n".join(f"- {f}" for f in flags))
        if pg["error"]:
            st.error(pg["error"])
        if not ex:
            st.info(f"Status: {pg['status']}. Results appear here when the worker gets to it.")
        for col, e in zip(st.columns(max(1, len(ex))), ex):
            with col:
                st.markdown(f"**{e['model']}** · {(e['ms'] or 0) / 1000:.0f}s")
                if not e["ok"]:
                    st.error(e["error"])
                    continue
                d = json.loads(e["data"])
                st.caption(f"jurisdiction: {d.get('jurisdiction') or '—'}")
                st.write(f"Units ({len(d['units'])})")
                st.dataframe(pd.DataFrame(d["units"]), width="stretch", hide_index=True)
                st.write(f"Positions ({len(d['positions'])})")
                st.dataframe(pd.DataFrame(d["positions"]), width="stretch", hide_index=True)
        with st.expander("Captured text (what the models saw)"):
            st.text(pg["text"][:30000])
        if st.button("Re-parse this page"):
            c = conn()
            with c:
                c.execute("DELETE FROM extraction WHERE page_id=?", (pid,))
                c.execute("UPDATE page SET status='pending', error=NULL WHERE id=?", (pid,))
            c.close()
            st.rerun()

# ---- model comparison ----------------------------------------------------
with tab_cmp:
    if len(models) < 2:
        st.info("Run the daemon with two models to compare them, e.g. "
                "`--models \"qwen3.6,<second model>\"`. Facts both models find independently are more trustworthy.")
    else:
        a, b = st.columns(2)
        ma = a.selectbox("Model A", models, index=0)
        mb = b.selectbox("Model B", models, index=1)
        c = conn()
        rows = c.execute("""SELECT p.id, p.title, ea.data AS da, eb.data AS db FROM page p
                            JOIN extraction ea ON ea.page_id=p.id AND ea.model=? AND ea.ok=1 AND ea.text_hash=p.text_hash
                            JOIN extraction eb ON eb.page_id=p.id AND eb.model=? AND eb.ok=1 AND eb.text_hash=p.text_hash
                            ORDER BY p.id DESC""", (ma, mb)).fetchall()
        c.close()
        if not rows:
            st.info("No pages parsed by both models yet.")
        else:
            results = {r["id"]: H.compare(json.loads(r["da"]), json.loads(r["db"])) for r in rows}
            df = pd.DataFrame([{"page": r["id"], "title": r["title"][:70],
                                **{k: results[r["id"]][k]["agreement"] for k in ("units", "positions", "contacts")}}
                               for r in rows])
            m1, m2, m3 = st.columns(3)
            m1.metric("Units agreement", f"{df.units.mean():.0%}")
            m2.metric("Positions agreement", f"{df.positions.mean():.0%}")
            m3.metric("Contacts agreement", f"{df.contacts.mean():.0%}")
            st.dataframe(df.sort_values("positions"), width="stretch", hide_index=True)
            pick = st.selectbox("Where do they disagree?", df["page"].tolist(),
                                format_func=lambda i: f"#{i} {df.loc[df.page == i, 'title'].iloc[0]}")
            for cat in ("units", "positions", "contacts"):
                r = results[pick][cat]
                if r["only_a"] or r["only_b"]:
                    st.markdown(f"**{cat}**: {r['both']} agreed")
                    x, y = st.columns(2)
                    x.write(f"only {ma}"); x.write(r["only_a"] or "—")
                    y.write(f"only {mb}"); y.write(r["only_b"] or "—")
