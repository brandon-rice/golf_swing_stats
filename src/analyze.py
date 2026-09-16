"""Per-club descriptive stats -> text report.

For every club present in the shots table, report the mean and sample
standard deviation (ddof=1) of each shot metric, plus a left/right side
tendency derived from offline_yd (negative = left of target, positive =
right, matching the parser's sign convention).

Run:  python -m src.analyze [--out PATH] [--club CODE ...]
"""
from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.colors import qualitative

from . import config, db

# Base font size for the Plotly figures so chart text reads well on the
# dashboard (axis titles/ticks bump up from this).
CHART_FONT_SIZE = 15

# (column, label) in report order. The first 8 are the user's preferred order;
# the remainder follow in any order. Decimals chosen per metric below.
METRICS: list[tuple[str, str]] = [
    ("carry_yd", "Carry (yd)"),
    ("total_yd", "Total (yd)"),
    ("face_to_target_deg", "Face-to-target (deg)"),
    ("offline_yd", "Offline (yd)"),
    ("smash_factor", "Smash factor"),
    ("club_speed_mph", "Club speed (mph)"),
    ("ball_speed_mph", "Ball speed (mph)"),
    ("path_deg", "Club path (deg)"),
    # --- the rest (order not significant) ---
    ("launch_deg", "Launch (deg)"),
    ("back_spin_rpm", "Back spin (rpm)"),
    ("side_spin_rpm", "Side spin (rpm)"),
    ("side_angle_deg", "Side angle (deg)"),
    ("roll_yd", "Roll (yd)"),
    ("height_yd", "Apex height (yd)"),
    ("descent_deg", "Descent (deg)"),
    ("flight_sec", "Flight (sec)"),
    ("shot_score", "Shot score"),
    ("face_to_path_deg", "Face-to-path (deg)"),
]

# Decimal places per metric; default is 1.
DECIMALS: dict[str, int] = {
    "smash_factor": 2,
    "back_spin_rpm": 0,
    "side_spin_rpm": 0,
    "shot_score": 1,
}

DEFAULT_OUT = config.PROJECT_ROOT / "reports" / "club_descriptive_stats.txt"
DEFAULT_CHART = config.PROJECT_ROOT / "reports" / "shot_dispersion.html"


def load_shots(engine) -> pd.DataFrame:
    sql = (
        "SELECT s.*, c.club_name, c.sort_order "
        "FROM shots s JOIN clubs c ON c.club_code = s.club_code"
    )
    df = pd.read_sql(sql, engine)
    # Numeric metrics may arrive as object/Decimal from Postgres NUMERIC; coerce.
    for col, _ in METRICS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _fmt(value: float | None, col: str) -> str:
    if value is None or pd.isna(value):
        return "n/a"
    return f"{value:.{DECIMALS.get(col, 1)}f}"


def _side_summary(offline: pd.Series) -> str:
    vals = offline.dropna()
    if vals.empty:
        return "no offline data"
    left = int((vals < 0).sum())
    right = int((vals > 0).sum())
    center = int((vals == 0).sum())
    mean = vals.mean()
    if mean < -0.05:
        tend = "LEFT"
    elif mean > 0.05:
        tend = "RIGHT"
    else:
        tend = "CENTER"
    return (
        f"{tend} (avg {mean:+.1f} yd) | "
        f"Left: {left}, Right: {right}, Center: {center}"
    )


def _club_block(df_club: pd.DataFrame, code: str, name: str) -> list[str]:
    n = len(df_club)
    lines = [
        f"=== {code} - {name}  (n={n}) ===",
        f"  {'Metric':<22}{'Avg':>12}{'Std Dev':>12}",
        f"  {'-' * 46}",
    ]
    for col, label in METRICS:
        if col not in df_club.columns:
            continue
        series = df_club[col].dropna()
        avg = series.mean() if not series.empty else None
        # Sample std needs >=2 points.
        std = series.std(ddof=1) if len(series) >= 2 else None
        lines.append(f"  {label:<22}{_fmt(avg, col):>12}{_fmt(std, col):>12}")
    lines.append(f"  Side tendency: {_side_summary(df_club['offline_yd'])}")
    lines.append("")
    return lines


def build_report(df: pd.DataFrame, clubs: list[str] | None = None) -> str:
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = [
        "GOLF SWING - PER-CLUB DESCRIPTIVE STATS",
        f"Generated: {now}",
        f"Schema: {config.db_schema()}",
        f"Sessions: {df['session_id'].nunique()}   Total shots: {len(df)}",
        "Std Dev is sample (ddof=1); shown as 'n/a' when n<2 or no data.",
        "Offline sign: negative = left of target, positive = right.",
        "=" * 50,
        "",
    ]

    # Order clubs by their seeded sort_order.
    order = (
        df[["club_code", "club_name", "sort_order"]]
        .drop_duplicates()
        .sort_values("sort_order")
    )
    if clubs:
        wanted = {c.upper() for c in clubs}
        order = order[order["club_code"].isin(wanted)]

    body: list[str] = []
    for _, row in order.iterrows():
        code = row["club_code"]
        df_club = df[df["club_code"] == code]
        body.extend(_club_block(df_club, code, row["club_name"]))

    if not body:
        body = ["(no shots found for the requested clubs)", ""]

    return "\n".join(header + body)


def _cov_ellipse(
    x: np.ndarray, y: np.ndarray, n_std: float, n_points: int = 80
) -> tuple[np.ndarray, np.ndarray] | None:
    """Return (x, y) points of an n_std covariance ellipse, or None if the
    cloud is degenerate (too few points / collinear)."""
    if len(x) < 3:
        return None
    cov = np.cov(x, y)
    vals, vecs = np.linalg.eigh(cov)  # ascending eigenvalues
    if np.any(vals <= 0):
        return None
    order = vals.argsort()[::-1]
    vals, vecs = vals[order], vecs[:, order]
    theta = np.arctan2(vecs[1, 0], vecs[0, 0])
    a, b = n_std * np.sqrt(vals)
    t = np.linspace(0, 2 * np.pi, n_points)
    ex, ey = a * np.cos(t), b * np.sin(t)
    cos_th, sin_th = np.cos(theta), np.sin(theta)
    xr = x.mean() + ex * cos_th - ey * sin_th
    yr = y.mean() + ex * sin_th + ey * cos_th
    return xr, yr


def build_chart(
    df: pd.DataFrame,
    distance_col: str = "total_yd",
    clubs: list[str] | None = None,
    ellipses: bool = True,
) -> go.Figure:
    """Dispersion scatter: lateral offline (x) vs downrange distance (y).

    Each club is its own color; a black 'x' marks each club's mean landing
    spot. When ``ellipses`` is on, a solid 1-std and dashed 2-std covariance
    ellipse are drawn per club (needs >=3 shots). The dashed vertical line at
    x=0 is the target/aim line. Axes are locked to equal scale so the spread
    is true-to-life. Clicking a club in the legend toggles its dots, mean,
    and ellipses together.
    """
    order = (
        df[["club_code", "club_name", "sort_order"]]
        .drop_duplicates()
        .sort_values("sort_order")
    )
    if clubs:
        wanted = {c.upper() for c in clubs}
        order = order[order["club_code"].isin(wanted)]

    palette = qualitative.Plotly
    fig = go.Figure()
    max_abs_x = 1.0
    for i, (_, row) in enumerate(order.iterrows()):
        code = row["club_code"]
        color = palette[i % len(palette)]
        g = df[df["club_code"] == code]
        x = pd.to_numeric(g["offline_yd"], errors="coerce")
        y = pd.to_numeric(g[distance_col], errors="coerce")
        mask = x.notna() & y.notna()
        x, y = x[mask], y[mask]
        if x.empty:
            continue
        xv, yv = x.to_numpy(), y.to_numpy()
        max_abs_x = max(max_abs_x, float(np.abs(xv).max()))

        fig.add_trace(
            go.Scatter(
                x=x, y=y, mode="markers", name=f"{code} ({len(x)})",
                legendgroup=code, marker=dict(size=9, opacity=0.7, color=color),
                hovertemplate=(
                    f"{code}<br>offline %{{x:+.1f}} yd"
                    f"<br>{distance_col.replace('_yd','')} %{{y:.1f}} yd<extra></extra>"
                ),
            )
        )
        fig.add_trace(
            go.Scatter(
                x=[xv.mean()], y=[yv.mean()], mode="markers",
                legendgroup=code, showlegend=False,
                marker=dict(size=14, symbol="x", color="black", line=dict(width=1)),
                hovertemplate=f"{code} mean<br>offline %{{x:+.1f}} yd<br>%{{y:.1f}} yd<extra></extra>",
            )
        )
        if ellipses:
            for n_std, dash in ((1.0, "solid"), (2.0, "dash")):
                pts = _cov_ellipse(xv, yv, n_std)
                if pts is None:
                    continue
                ex, ey = pts
                fig.add_trace(
                    go.Scatter(
                        x=ex, y=ey, mode="lines", legendgroup=code, showlegend=False,
                        line=dict(color=color, width=1.5, dash=dash),
                        opacity=0.5, hoverinfo="skip",
                        name=f"{code} {int(n_std)}σ",
                    )
                )

    fig.add_vline(x=0, line=dict(color="gray", dash="dash"))
    pad = max_abs_x * 0.15 + 2
    fig.update_layout(
        title=(
            f"Shot Dispersion — offline vs {distance_col.replace('_yd', '')} distance"
            "<br><sub>solid = 1σ ellipse · dashed = 2σ · ✕ = mean</sub>"
        ),
        xaxis_title="Offline (yd)   ← left | right →",
        yaxis_title=f"{distance_col.replace('_yd', '').title()} distance (yd)",
        template="plotly_white",
        legend_title="Club",
        font=dict(size=CHART_FONT_SIZE),
        # Equal aspect (below) makes the plot area as tall as it is wide in
        # yards, so a taller figure is needed to keep it a usable size.
        height=720,
    )
    # Lock equal aspect so lateral spread isn't visually exaggerated, and keep
    # the offline range honest while doing it. ``constrain="domain"`` is what
    # makes the two compatible: without it Plotly satisfies the equal-scale
    # constraint by widening the x *range* (a wide, short plot area would stretch
    # offline out past ±200 yd), instead of by shrinking the plot area to fit.
    fig.update_xaxes(
        range=[-(max_abs_x + pad), max_abs_x + pad],
        zeroline=False,
        constrain="domain",
    )
    fig.update_yaxes(scaleanchor="x", scaleratio=1, constrain="domain")
    return fig


def _session_date_ticks(fig: go.Figure, session_ts: pd.Series) -> None:
    """Put one x-axis tick on each session date, labelled like ``Sep 14``.

    The year is added under the first label and wherever it changes. Several
    sessions on one calendar day share a single tick (at the day's first
    session) so dates never repeat.
    """
    ts = pd.to_datetime(session_ts).sort_values()
    firsts = ts.groupby(ts.dt.date).first()
    tickvals, ticktext, prev_year = [], [], None
    for t in firsts:
        label = t.strftime("%b ") + str(t.day)
        if t.year != prev_year:
            label += f"<br>{t.year}"
            prev_year = t.year
        tickvals.append(t)
        ticktext.append(label)
    fig.update_xaxes(type="date", tickmode="array", tickvals=tickvals, ticktext=ticktext)


def build_session_trend(
    df_club: pd.DataFrame,
    distance_col: str = "total_yd",
    label: str = "Total (yd)",
) -> go.Figure:
    """Per-session mean of ``distance_col`` for one club over time.

    Each point is a session's mean distance; the error bars are that session's
    sample standard deviation (omitted for single-shot sessions). Useful for
    spotting whether a club's distance is trending up/down and tightening.
    """
    y = pd.to_numeric(df_club[distance_col], errors="coerce")
    g = (
        pd.DataFrame({"session_ts": df_club["session_ts"], "y": y})
        .dropna(subset=["y"])
        .groupby("session_ts")["y"]
        .agg(["mean", "std"])
        .reset_index()
        .sort_values("session_ts")
    )

    fig = go.Figure()
    if not g.empty:
        fig.add_trace(
            go.Scatter(
                x=g["session_ts"],
                y=g["mean"],
                mode="lines+markers",
                error_y=dict(type="data", array=g["std"].fillna(0.0), visible=True),
                marker=dict(size=9),
                line=dict(width=2),
                hovertemplate=(
                    "%{x|%b %-d, %Y}<br>mean %{y:.1f} yd"
                    "<br>σ %{error_y.array:.1f} yd<extra></extra>"
                ),
            )
        )
    fig.update_layout(
        title=f"{label} by date",
        xaxis_title="Date",
        yaxis_title=label,
        template="plotly_white",
        font=dict(size=CHART_FONT_SIZE),
    )
    _session_date_ticks(fig, g["session_ts"])
    return fig


def build_shots_per_session(sessions: pd.DataFrame) -> go.Figure:
    """Bar chart of shot count per session, x-axis as calendar dates.

    ``sessions`` is the one-row-per-session frame (``session_ts`` + ``shots``).
    """
    g = sessions[["session_ts", "shots"]].sort_values("session_ts")
    fig = go.Figure(
        go.Bar(
            x=g["session_ts"],
            y=g["shots"],
            hovertemplate="%{x|%b %-d, %Y}<br>%{y} shots<extra></extra>",
        )
    )
    fig.update_layout(
        xaxis_title="Date",
        yaxis_title="Shots",
        template="plotly_white",
        font=dict(size=CHART_FONT_SIZE),
    )
    _session_date_ticks(fig, g["session_ts"])
    return fig


def build_clubs_trend(trend: pd.DataFrame, label: str = "value") -> go.Figure:
    """Line-per-club trend over time, x-axis as calendar dates.

    ``trend`` is a wide frame indexed by ``session_ts`` with one column per club
    code (the value is that session's mean of the chosen metric). Missing values
    are gaps in the line (the club wasn't hit that session).
    """
    palette = qualitative.Plotly
    fig = go.Figure()
    for i, code in enumerate(trend.columns):
        series = trend[code]
        fig.add_trace(
            go.Scatter(
                x=trend.index,
                y=series,
                mode="lines+markers",
                name=str(code),
                connectgaps=False,
                marker=dict(size=8, color=palette[i % len(palette)]),
                line=dict(width=2, color=palette[i % len(palette)]),
                hovertemplate=f"{code}<br>%{{x|%b %-d, %Y}}<br>%{{y:.1f}}<extra></extra>",
            )
        )
    fig.update_layout(
        xaxis_title="Date",
        yaxis_title=label,
        template="plotly_white",
        legend_title="Club",
        font=dict(size=CHART_FONT_SIZE),
    )
    _session_date_ticks(fig, pd.Series(trend.index))
    return fig


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Per-club descriptive stats to a text file.")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help=f"Text report (default: {DEFAULT_OUT}).")
    ap.add_argument("--club", nargs="*", help="Limit to specific club codes (e.g. 7I PW D).")
    ap.add_argument("--chart-out", default=str(DEFAULT_CHART), help=f"Dispersion chart HTML (default: {DEFAULT_CHART}).")
    ap.add_argument("--distance", choices=("total", "carry"), default="total", help="Downrange axis for the chart (default: total).")
    ap.add_argument("--no-chart", action="store_true", help="Skip the dispersion chart.")
    ap.add_argument("--no-ellipses", action="store_true", help="Omit the 1σ/2σ dispersion ellipses.")
    args = ap.parse_args(argv)

    df = load_shots(db.local_engine())
    if df.empty:
        print("No shots in the database — nothing to analyze.")
        return 1

    report = build_report(df, args.club)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nWrote report to {out}")

    if not args.no_chart:
        fig = build_chart(
            df, distance_col=f"{args.distance}_yd", clubs=args.club,
            ellipses=not args.no_ellipses,
        )
        chart_out = Path(args.chart_out)
        chart_out.parent.mkdir(parents=True, exist_ok=True)
        fig.write_html(str(chart_out), include_plotlyjs="cdn")
        print(f"Wrote dispersion chart to {chart_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
