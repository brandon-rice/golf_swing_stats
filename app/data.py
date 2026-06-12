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
build_session_trend = analyze.build_session_trend
METRICS = analyze.METRICS
DECIMALS = analyze.DECIMALS
side_summary = analyze._side_summary

# ---------------------------------------------------------------------------
# Data source selection
# ---------------------------------------------------------------------------
#
# The app reads exclusively from the Neon cloud mirror — that's the only target
# reachable once it's deployed to Streamlit Cloud. (The local Postgres engine is
# still used by the ``src`` ingest pipeline, just not by the dashboard.)


@st.cache_resource(show_spinner=False)
def _engine(source: str):
    return db.neon_engine()


def source_selector() -> str:
    """Confirm Neon is configured, note it in the sidebar, and return ``"Neon"``.

    Kept as a function (returning a source string) so the pages can keep passing
    ``source`` through to the cached loaders unchanged — it's just always Neon.
    """
    try:
        config.neon_db_url()
    except Exception:
        st.error(
            "Neon is not configured. Set `NEON_DB_URL` in `.env` / Streamlit "
            "secrets."
        )
        st.stop()

    st.session_state["source"] = "Neon"
    st.sidebar.caption("Data source: **Neon**")
    return "Neon"


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


# Shot columns already covered by the explicit base columns below, so they are
# not repeated when ``extended=True`` appends the remaining metrics.
_AVG_BASE_COLS = {
    "carry_yd", "total_yd", "offline_yd", "face_to_target_deg", "path_deg",
    "smash_factor", "club_speed_mph", "ball_speed_mph", "launch_deg",
}


def _mean(series: pd.Series, dp: int) -> float | None:
    s = series.dropna()
    return None if s.empty else round(s.mean(), dp)


def _std(series: pd.Series) -> float | None:
    s = series.dropna()
    return s.std(ddof=1) if len(s) >= 2 else None


def _fmt(value: float | None, dp: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{dp}f}"


def club_averages(df: pd.DataFrame, extended: bool = False) -> pd.DataFrame:
    """Per-club summary table indexed by club code, in sort order.

    Distances are means; ``Total σ`` / ``Offline σ`` are sample std devs and the
    67% / 95% columns are the mean ± 1σ / ± 2σ total-yardage bands shown as
    ``low – high`` strings (``"n/a"`` when a club has fewer than two shots). With
    ``extended=True`` every remaining metric mean is appended after the base set.
    """
    rows = []
    for code in club_order(df):
        g = df[df["club_code"] == code]
        total = g["total_yd"].dropna()
        t_mean = total.mean() if not total.empty else None
        t_std = total.std(ddof=1) if len(total) >= 2 else None

        def band(k: float) -> str:
            if t_mean is None or t_std is None:
                return "n/a"
            return f"{t_mean - k * t_std:.1f} – {t_mean + k * t_std:.1f}"

        row: dict[str, object] = {
            "Club": code,
            "N": len(g),
            "Carry (yds)": _mean(g["carry_yd"], 1),
            "Total (yds)": None if t_mean is None else round(t_mean, 1),
            "Total σ": _fmt(t_std, 1),
            "67% (yds)": band(1),
            "95% (yds)": band(2),
            "Offline (yds)": _mean(g["offline_yd"], 1),
            "Offline σ": _fmt(_std(g["offline_yd"]), 1),
            "Face-to-target (deg)": _mean(g["face_to_target_deg"], 1),
            "Club path (deg)": _mean(g["path_deg"], 1),
            "Smash factor": _mean(g["smash_factor"], 2),
            "Club speed (mph)": _mean(g["club_speed_mph"], 1),
            "Ball speed (mph)": _mean(g["ball_speed_mph"], 1),
            "Launch (deg)": _mean(g["launch_deg"], 1),
        }
        if extended:
            for col, label in METRICS:
                if col in _AVG_BASE_COLS or col not in g.columns:
                    continue
                row[label] = _mean(g[col], DECIMALS.get(col, 1))
        rows.append(row)

    return pd.DataFrame(rows).set_index("Club")


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
        int(r.session_id): f"{r.session_ts:%Y-%m-%d %H:%M}"
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
