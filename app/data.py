"""Shared data access for the Streamlit dashboard.

This module is imported by every page (the app directory is on ``sys.path``
when Streamlit runs). It inserts the project root onto the path so the ``src``
package is importable, picks the active Postgres target (Local or Neon),
and exposes cached loaders plus the chart/report builders from
:mod:`src.analyze`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import streamlit as st

# Make the project root importable so ``from src import ...`` works regardless
# of where Streamlit is launched from.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from src import analyze, config, db  # noqa: E402

# Re-export the builders so pages can import everything from one place.
build_chart = analyze.build_chart
build_report = analyze.build_report
METRICS = analyze.METRICS
side_summary = analyze._side_summary

# ---------------------------------------------------------------------------
# Data source selection
# ---------------------------------------------------------------------------

#: Logical name -> (label, availability check) for the two Postgres targets.
_SOURCES = ("Local", "Neon")


def _available_sources() -> list[str]:
    """Sources whose connection settings are present (so we don't offer a
    target that will only error on first query)."""
    avail: list[str] = []
    try:
        config.local_db_url()
        avail.append("Local")
    except Exception:
        pass
    try:
        config.neon_db_url()
        avail.append("Neon")
    except Exception:
        pass
    return avail


@st.cache_resource(show_spinner=False)
def _engine(source: str):
    return db.neon_engine() if source == "Neon" else db.local_engine()


def source_selector() -> str:
    """Render the data-source picker in the sidebar and return the choice.

    The selection persists across pages via ``st.session_state``. If only one
    target is configured it is used silently; if none are, the app stops with
    a clear message.
    """
    avail = _available_sources()
    if not avail:
        st.error(
            "No database configured. Set local `DB_*` keys or `NEON_DB_URL` "
            "in `.env` / Streamlit secrets."
        )
        st.stop()

    if len(avail) == 1:
        st.session_state["source"] = avail[0]
        st.sidebar.caption(f"Data source: **{avail[0]}**")
        return avail[0]

    default = st.session_state.get("source", avail[0])
    choice = st.sidebar.radio(
        "Data source",
        avail,
        index=avail.index(default) if default in avail else 0,
        help="Local Postgres or the Neon cloud mirror.",
    )
    st.session_state["source"] = choice
    return choice


# ---------------------------------------------------------------------------
# Cached loaders
# ---------------------------------------------------------------------------


@st.cache_data(ttl=300, show_spinner="Loading shots…")
def load_shots(source: str) -> pd.DataFrame:
    """Every shot joined to its club and session, numeric metrics coerced."""
    sql = (
        "SELECT sh.*, c.club_name, c.club_type, c.sort_order, "
        "       se.session_ts, se.source_file "
        "FROM shots sh "
        "JOIN clubs c ON c.club_code = sh.club_code "
        "JOIN sessions se ON se.session_id = sh.session_id"
    )
    df = pd.read_sql(sql, _engine(source))
    for col, _ in METRICS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "session_ts" in df.columns:
        df["session_ts"] = pd.to_datetime(df["session_ts"])
    return df


@st.cache_data(ttl=300, show_spinner="Loading sessions…")
def load_sessions(source: str) -> pd.DataFrame:
    """One row per session with shot/club counts, newest first."""
    sql = (
        "SELECT se.session_id, se.session_ts, se.source_file, se.player_name, "
        "       se.imported_at, se.published_at, "
        "       COUNT(sh.shot_id) AS shots, "
        "       COUNT(DISTINCT sh.club_code) AS clubs "
        "FROM sessions se "
        "LEFT JOIN shots sh ON sh.session_id = se.session_id "
        "GROUP BY se.session_id "
        "ORDER BY se.session_ts DESC"
    )
    df = pd.read_sql(sql, _engine(source))
    if "session_ts" in df.columns:
        df["session_ts"] = pd.to_datetime(df["session_ts"])
    return df


def club_order(df: pd.DataFrame) -> list[str]:
    """Club codes present in ``df`` ordered by their seeded ``sort_order``."""
    order = (
        df[["club_code", "sort_order"]]
        .drop_duplicates()
        .sort_values("sort_order")
    )
    return order["club_code"].tolist()


def filter_sidebar(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Render club + session sidebar filters and return ``(filtered_df, clubs)``.

    ``clubs`` is the selected club codes in sort order (empty list means "all",
    which the analyze builders interpret as no club restriction).
    """
    st.sidebar.subheader("Filters")

    sessions = (
        df[["session_id", "session_ts"]]
        .drop_duplicates()
        .sort_values("session_ts", ascending=False)
    )
    labels = {
        int(r.session_id): f"{r.session_ts:%Y-%m-%d %H:%M}  (#{int(r.session_id)})"
        for r in sessions.itertuples()
    }
    picked_sessions = st.sidebar.multiselect(
        "Sessions",
        options=list(labels),
        format_func=lambda sid: labels[sid],
        default=[],
        help="Leave empty to include every session.",
    )

    all_clubs = club_order(df)
    picked_clubs = st.sidebar.multiselect(
        "Clubs",
        options=all_clubs,
        default=[],
        help="Leave empty to include every club.",
    )

    out = df
    if picked_sessions:
        out = out[out["session_id"].isin(picked_sessions)]
    if picked_clubs:
        out = out[out["club_code"].isin(picked_clubs)]
    return out, picked_clubs
