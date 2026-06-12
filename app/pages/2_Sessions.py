"""Sessions overview: the list of ingested sessions and how a chosen metric
trends per club across sessions over time."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import data

st.set_page_config(page_title="Sessions", page_icon="🗓️", layout="wide")
st.title("🗓️ Sessions")

source = data.source_selector()
sessions = data.load_sessions(source)

if sessions.empty:
    st.info("No sessions found in this database yet.")
    st.stop()

# --- KPI row ----------------------------------------------------------------
k1, k2, k3 = st.columns(3)
k1.metric("Sessions", len(sessions))
k2.metric("Total shots", int(sessions["shots"].sum()))
days_since = (
    pd.Timestamp.now().normalize() - sessions["session_ts"].max().normalize()
).days
k3.metric("Days since last session", days_since)

# --- Session list ----------------------------------------------------------
st.subheader("Ingested sessions")
table = sessions.assign(published=sessions["published_at"].notna())[
    ["session_id", "session_ts", "shots", "clubs", "source_file", "published"]
]
st.dataframe(
    table,
    use_container_width=True,
    hide_index=True,
    column_config={
        "session_id": st.column_config.NumberColumn("ID", format="%d"),
        "session_ts": st.column_config.DatetimeColumn("When", format="YYYY-MM-DD HH:mm"),
        "published": st.column_config.CheckboxColumn("On Neon"),
    },
)

# --- Shots per session ------------------------------------------------------
st.subheader("Shots per session")
st.bar_chart(
    sessions.set_index("session_ts")["shots"].sort_index(),
    y_label="Shots",
    x_label="Session",
)

# --- Trend over sessions ---------------------------------------------------
st.subheader("Trend across sessions")
shots = data.load_shots(source)
if shots.empty:
    st.stop()

metric_label = {label: col for col, label in data.METRICS}
col1, col2 = st.columns([1, 3])
choice = col1.selectbox("Metric", list(metric_label), index=0)
metric = metric_label[choice]

all_clubs = data.club_order(shots)
default = all_clubs[: min(3, len(all_clubs))]
picked = col2.multiselect("Clubs", all_clubs, default=default)

if not picked:
    st.info("Pick at least one club to plot a trend.")
    st.stop()

sub = shots[shots["club_code"].isin(picked)]
trend = (
    sub.groupby([sub["session_ts"], "club_code"])[metric]
    .mean()
    .unstack("club_code")
    .sort_index()
)
trend = trend.reindex(columns=picked)  # stable, sorted club order

st.line_chart(trend, y_label=choice, x_label="Session")
st.caption(
    f"Per-session mean **{choice}** by club. Each point is one session's "
    "average for that club; gaps mean the club wasn't hit that session."
)
