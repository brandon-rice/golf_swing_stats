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
build_shots_per_session = analyze.build_shots_per_session
build_clubs_trend = analyze.build_clubs_trend
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
    "carry_yd", "total_yd", "offline_yd", "face_to_target_deg",
    "club_speed_mph",
}


def _mean(series: pd.Series, dp: int) -> float | None:
    s = series.dropna()
    return None if s.empty else round(s.mean(), dp)


def _std(series: pd.Series) -> float | None:
    s = series.dropna()
    return s.std(ddof=1) if len(s) >= 2 else None


def _fmt(value: float | None, dp: int = 1) -> str:
    return "n/a" if value is None else f"{value:.{dp}f}"


def club_averages(
    df: pd.DataFrame, extended: bool = False, show_95: bool = True
) -> pd.DataFrame:
    """Per-club summary table indexed by club code, in sort order.

    Distances are means; ``Total σ`` / ``Offline σ`` are sample std devs and the
    67% / 95% columns are the mean ± 1σ / ± 2σ total-yardage bands shown as
    ``low – high`` strings (``"n/a"`` when a club has fewer than two shots). With
    ``extended=True`` every remaining metric mean is appended after the base set;
    ``show_95=False`` drops the 2σ band for the narrower Overview table.
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
            **({"95% (yds)": band(2)} if show_95 else {}),
            "Offline (yds)": _mean(g["offline_yd"], 1),
            "Offline σ": _fmt(_std(g["offline_yd"]), 1),
            "Face-to-target (deg)": _mean(g["face_to_target_deg"], 1),
            "Club speed (mph)": _mean(g["club_speed_mph"], 1),
        }
        if extended:
            for col, label in METRICS:
                if col in _AVG_BASE_COLS or col not in g.columns:
                    continue
                row[label] = _mean(g[col], DECIMALS.get(col, 1))
        rows.append(row)

    return pd.DataFrame(rows).set_index("Club")


# ---------------------------------------------------------------------------
# Outlier detection
# ---------------------------------------------------------------------------
#
# Nothing is ever deleted from Postgres — shots are flagged here, at query
# time, so the filter can be switched off and re-tuned without re-ingesting.
#
# Two layers, because there are two different problems:
#
#   1. Physical gates catch shots that were not real swings at all (tapping the
#      ball, a topped shot that rolled). These are deterministic.
#   2. A robust MAD test catches real-but-unrepresentative strikes on the low
#      distance tail. MAD is used rather than a z-score because the standard
#      deviation is itself inflated by the outliers being looked for.
#
# Only the *low* tail is tested, and only on distance: offline yardage is
# deliberately left alone, since shot dispersion is what the chart exists to
# show and trimming it would flatter the numbers.

_MIN_CLUB_SPEED_FRAC = 0.6   # of the club's median club speed
_MAX_CARRY_RATIO = 0.5       # carry / total below this reads as a topped shot
_MAD_Z = -3.5                # modified z-score cutoff, low tail only
_MAD_MIN_N = 12              # clubs with fewer shots skip the statistical test
_MAD_SCALE = 0.6745          # 0.75 quantile of the normal distribution


def _mad_low_tail(totals: pd.Series) -> pd.Series:
    """Boolean mask of ``totals`` sitting far below the median (modified z).

    Returns all-``False`` for clubs with too few shots to judge, or when the
    MAD is zero (every shot identical), which would divide by zero.
    """
    if len(totals.dropna()) < _MAD_MIN_N:
        return pd.Series(False, index=totals.index)
    med = totals.median()
    mad = (totals - med).abs().median()
    if not mad:
        return pd.Series(False, index=totals.index)
    return _MAD_SCALE * (totals - med) / mad < _MAD_Z


def outlier_reasons(df: pd.DataFrame) -> pd.Series:
    """Why each shot is an outlier, or ``""`` for shots that are kept.

    Reasons are assigned in priority order, so a shot that trips more than one
    rule reports the most specific explanation.
    """
    reason = pd.Series("", index=df.index, dtype="object")

    def mark(mask: pd.Series, text: str) -> None:
        reason[mask.fillna(False) & (reason == "")] = text

    # 1. Physical gates.
    club_speed_median = df.groupby("club_code")["club_speed_mph"].transform("median")
    mark(
        df["club_speed_mph"] < _MIN_CLUB_SPEED_FRAC * club_speed_median,
        "not a full swing",
    )
    carry_ratio = df["carry_yd"] / df["total_yd"].where(df["total_yd"] > 0)
    mark(carry_ratio < _MAX_CARRY_RATIO, "topped — mostly roll")

    # 2. Robust distance test, per club.
    mark(
        df.groupby("club_code")["total_yd"].transform(_mad_low_tail),
        "far below club distance",
    )
    return reason


def outlier_sidebar(df: pd.DataFrame) -> pd.DataFrame:
    """Render the outlier toggle and return ``df`` with outliers dropped.

    The excluded shots stay visible in an expander so the filter never silently
    removes something without showing what it took.
    """
    st.sidebar.markdown("**Shots**")
    if not st.sidebar.checkbox(
        "Exclude mishits",
        value=True,
        help=(
            "Drops non-swings (taps, topped shots) and strikes far below the "
            "club's normal distance. Never deletes anything from the database."
        ),
    ):
        return df

    reason = outlier_reasons(df)
    dropped = reason != ""
    n = int(dropped.sum())
    if not n:
        st.sidebar.caption("No outliers in range")
        return df

    st.sidebar.caption(f"Excluding {n} shot{'' if n == 1 else 's'} ({n / len(df):.1%})")
    with st.sidebar.expander(f"Show {n} excluded"):
        shown = df[dropped].assign(Why=reason[dropped])
        shown = shown.sort_values("total_yd")[
            ["session_ts", "club_code", "total_yd", "Why"]
        ]
        st.dataframe(
            shown,
            hide_index=True,
            column_config={
                "session_ts": st.column_config.DateColumn("Date", format="MM-DD"),
                "club_code": st.column_config.TextColumn("Club"),
                "total_yd": st.column_config.NumberColumn("Total", format="%d"),
            },
        )
    return df[~dropped]


# Date-range presets. "Last N" counts sessions (not calendar days) because
# sessions are irregularly spaced; the day-based presets anchor to today so
# "30 days" means what it says even when the last range trip was a while ago.
_PRESETS = ("All", "Last 1", "Last 5", "30 days", "Custom")


def _preset_range(
    dates: pd.Series, preset: str
) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Resolve a preset label to an inclusive ``(start, end)`` of session dates.

    ``dates`` is the ascending series of distinct session dates.
    """
    first, last = dates.iloc[0], dates.iloc[-1]
    if preset == "Last 1":
        return last, last
    if preset == "Last 5":
        return dates.iloc[-min(5, len(dates))], last
    if preset == "30 days":
        cutoff = pd.Timestamp.now().normalize() - pd.Timedelta(days=30)
        # Never return an empty window: if nothing is that recent, fall back to
        # the most recent session alone.
        return (cutoff, last) if cutoff <= last else (last, last)
    return first, last


def date_sidebar(df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
    """Render the sidebar date filter and return ``(filtered_df, caption)``.

    A segmented preset control drives the common cases; picking ``Custom``
    reveals a start/end calendar clamped to the data. An expander underneath
    allows cherry-picking individual dates, which overrides the range when any
    are selected (the only way to express a non-contiguous span).
    """
    dates = (
        df["session_ts"].dt.normalize().drop_duplicates().sort_values()
        .reset_index(drop=True)
    )
    first, last = dates.iloc[0], dates.iloc[-1]

    st.sidebar.markdown("**Date range**")
    preset = st.sidebar.segmented_control(
        "Date range",
        options=_PRESETS,
        default="All",
        label_visibility="collapsed",
        key="date_preset",
    ) or "All"

    start, end = _preset_range(dates, preset)
    if preset == "Custom":
        picked = st.sidebar.date_input(
            "Start / end",
            value=(first.date(), last.date()),
            min_value=first.date(),
            max_value=last.date(),
            label_visibility="collapsed",
            key="date_custom",
        )
        # Mid-selection the widget hands back a 1-tuple; hold the old end until
        # the user clicks the second date.
        if isinstance(picked, (tuple, list)) and picked:
            start = pd.Timestamp(picked[0])
            end = pd.Timestamp(picked[-1]) if len(picked) > 1 else last

    exact = st.sidebar.expander("Pick specific dates")
    labels = {d: f"{d:%Y-%m-%d}" for d in dates}
    picked_dates = exact.multiselect(
        "Sessions",
        options=list(labels)[::-1],  # newest first
        format_func=lambda d: labels[d],
        default=[],
        label_visibility="collapsed",
        help="Overrides the range above when anything is selected.",
    )

    day = df["session_ts"].dt.normalize()
    if picked_dates:
        out = df[day.isin(picked_dates)]
        caption = f"{len(picked_dates)} selected date(s)"
    else:
        out = df[(day >= start) & (day <= end)]
        n = out["session_id"].nunique()
        caption = f"{n} session{'' if n == 1 else 's'} · {start:%b %d} → {end:%b %d %Y}"

    st.sidebar.caption(caption)
    return out, caption


def filter_sidebar(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Render date + club sidebar filters and return ``(filtered_df, clubs)``.

    ``clubs`` is the selected club codes in sort order (empty list means "all",
    which the analyze builders interpret as no club restriction).
    """
    st.sidebar.subheader("Filters")

    out, _ = date_sidebar(df)
    # Flag outliers against the selected date range, so a club's "normal"
    # distance is judged from the shots actually on screen.
    out = outlier_sidebar(out)

    all_clubs = club_order(df)
    picked_clubs = st.sidebar.multiselect(
        "Clubs",
        options=all_clubs,
        default=[],
        help="Leave empty to include every club.",
    )

    if picked_clubs:
        out = out[out["club_code"].isin(picked_clubs)]
    return out, picked_clubs
