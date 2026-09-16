"""Per-club descriptive statistics: a summary table across clubs, a single-club
detail (mean ± std for every metric), and the plain-text report download."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import data

st.set_page_config(page_title="Club Stats", page_icon="📊", layout="wide")
st.title("📊 Club Stats")

source = data.source_selector()
df = data.load_shots(source)

if df.empty:
    st.info("No shots found in this database yet.")
    st.stop()

view, _ = data.filter_sidebar(df)
if view.empty:
    st.warning("No shots match the current filters.")
    st.stop()

clubs = data.club_order(view)
names = dict(zip(view["club_code"], view["club_name"]))

# --- Summary: every stat per club ------------------------------------------
st.subheader("Averages by club")
st.dataframe(
    data.club_averages(view, extended=True),
    use_container_width=True,
    height="content",
)
st.caption(
    "Distances are means; Total σ / Offline σ are sample std devs and 67% / 95% "
    "are the mean ± 1σ / ± 2σ total-yardage bands. Detail view below has every "
    "metric's std dev."
)

# --- Dispersion chart ------------------------------------------------------
st.subheader("Shot dispersion")
opt1, opt2, _ = st.columns([1, 1, 4])
distance = opt1.radio("Downrange axis", ("total", "carry"), horizontal=True)
ellipses = opt2.toggle("1σ / 2σ ellipses", value=True)
st.plotly_chart(
    data.build_chart(
        view,
        distance_col=f"{distance}_yd",
        clubs=clubs or None,
        ellipses=ellipses,
    ),
    use_container_width=True,
)

# --- Single-club detail ----------------------------------------------------
st.subheader("Club detail")
code = st.selectbox(
    "Club", clubs, format_func=lambda c: f"{c} — {names.get(c, c)}"
)
g = view[view["club_code"] == code]

detail = []
for col, label in data.METRICS:
    series = g[col].dropna()
    avg = series.mean() if not series.empty else None
    std = series.std(ddof=1) if len(series) >= 2 else None
    detail.append({"Metric": label, "Avg": avg, "Std Dev": std})

st.markdown(f"**{code} — {names.get(code, code)}**  ·  n = {len(g)}")
d1, d2 = st.columns([2, 3])
with d1:
    st.dataframe(
        pd.DataFrame(detail).set_index("Metric").round(2),
        use_container_width=True,
    )
with d2:
    st.markdown("**Side tendency**")
    st.write(data.side_summary(g["offline_yd"]))

# --- Session-to-session trend (total distance) -----------------------------
st.markdown("**Total distance trend**")
st.plotly_chart(data.build_session_trend(g), use_container_width=True)
st.caption("Each point is a session's mean total yardage; error bars are that session's std dev.")

# --- Full text report download --------------------------------------------
report = data.build_report(view)
with st.expander("Full text report"):
    st.code(report, language="text")
st.download_button(
    "Download report (.txt)",
    report.encode("utf-8"),
    file_name="club_descriptive_stats.txt",
    mime="text/plain",
)
