"""Golf Swing Stats — Streamlit dashboard entry point (Overview).

Run from the project root:

    streamlit run app/Home.py
"""
from __future__ import annotations

import streamlit as st

import data

st.set_page_config(page_title="Golf Swing Stats", page_icon="⛳", layout="wide")

st.title("⛳ Golf Swing Stats")

source = data.source_selector()
df = data.load_shots(source)

if df.empty:
    st.info("No shots found in this database yet. Ingest a session and refresh.")
    st.stop()

view, clubs = data.filter_sidebar(df)

if view.empty:
    st.warning("No shots match the current filters.")
    st.stop()

# --- KPI row ---------------------------------------------------------------
c1, c2, c3, c4 = st.columns(4)
c1.metric("Shots", f"{len(view):,}")
c2.metric("Sessions", view["session_id"].nunique())
c3.metric("Clubs", view["club_code"].nunique())
span = f"{view['session_ts'].min():%Y-%m-%d} → {view['session_ts'].max():%Y-%m-%d}"
c4.metric("Date range", span)

# --- Dispersion chart ------------------------------------------------------
opt1, opt2, _ = st.columns([1, 1, 4])
distance = opt1.radio("Downrange axis", ("total", "carry"), horizontal=True)
ellipses = opt2.toggle("1σ / 2σ ellipses", value=True)

fig = data.build_chart(
    view,
    distance_col=f"{distance}_yd",
    clubs=clubs or None,
    ellipses=ellipses,
)
st.plotly_chart(fig, use_container_width=True)

# --- Raw data --------------------------------------------------------------
with st.expander(f"Shot data ({len(view):,} rows)"):
    st.dataframe(
        view.sort_values(["session_ts", "club_code", "shot_number"]),
        use_container_width=True,
        hide_index=True,
    )
    st.download_button(
        "Download CSV",
        view.to_csv(index=False).encode("utf-8"),
        file_name="shots.csv",
        mime="text/csv",
    )

st.caption(f"Reading from **{source}** · schema `{data.config.db_schema()}`")
