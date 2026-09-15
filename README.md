# golf_swing_stats

Personal pipeline for tracking and analyzing [SkyTrak](https://skytrak.golf/) launch-monitor
data. It parses SkyTrak shot-history CSV exports, loads them into Postgres, and produces
per-club descriptive stats and a shot-dispersion chart.

```
one or more export folders  →  parse  →  local Postgres  →  analyze (stats + chart)
                                              └─→ Neon cloud  →  Streamlit dashboard
```

## Features

- **Parser** (`src/parser.py`) — reads SkyTrak multi-club exports in **both export
  generations** (see below), tolerating short club codes (`PW`, `7I`), full names
  (`7 IRON`, `7 WOOD`), and wedge loft labels (`56°`, `60`), normalizing them all to
  canonical codes so one club never splits across two. Metric columns are located by
  the file's own two-row column header, not by position, so a column SkyTrak adds, drops, or
  renames doesn't silently shift every reading after it.
- **Ingest** (`src/ingest.py`) — scans **every configured data folder** in one pass and loads
  the files into Postgres, deduplicating by file hash so re-running is safe; each file lands
  in its own transaction.
- **Analyze** (`src/analyze.py`) — per-club mean/std-dev report plus an interactive Plotly
  dispersion chart with 1σ/2σ ellipses.
- **Publish** (`src/publish.py`) — copies local sessions to a Neon (cloud) Postgres database,
  idempotently, so the data can be reached from anywhere.
- **Dashboard** (`app/`) — a multipage Streamlit app (Overview, Club Stats, Sessions) that
  reads from either the local or the Neon database and renders the same dispersion chart and
  per-club stats interactively, with filters.

## Prerequisites

- Python 3.12+
- PostgreSQL 14+ running locally (developed against Postgres 18)
- A SkyTrak account that can export **ShotsHistory** CSV files

## Setup

```powershell
# 1. Clone
git clone https://github.com/<your-username>/golf_swing_stats.git
cd golf_swing_stats

# 2. Create and activate a virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows PowerShell
# source .venv/bin/activate          # macOS/Linux

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment
copy .env.example .env               # then edit .env with your DB + data-dir settings
```

Edit `.env` (see `.env.example` for all keys):

- `DB_HOST` / `DB_PORT` / `DB_NAME` / `DB_USER` / `DB_PASSWORD` — local Postgres connection
  (or set a single `LOCAL_DB_URL`).
- `DB_SCHEMA` — schema holding the golf tables (default `golf_swing_stats`).
- `SWING_DATA_DIRS` — one or more folders where your SkyTrak CSV exports live, separated by
  `;` on Windows (`:` on macOS/Linux). `SWING_DATA_DIR` (singular) still works for a single
  folder, and is merged in if both are set.

Create the schema, tables, and seed the club list (idempotent — safe to re-run):

```powershell
python -m src.ingest --init
```

## Supported export formats

SkyTrak changed its export part-way through, and the two generations land in different
folders with different filenames and a different column layout. Both are supported, and each
file is identified by its *contents*, never by its filename or folder — so the two can even
sit in the same folder.

| | Legacy | Current |
|---|---|---|
| Filename | `Export_ShotsHistory_06092026_134151.csv` | `activity-1788786050.csv` |
| Session header | `PRACTICE: 6/9/2026 1:41 PM` | `PRACTICE: 07/09/2026 • 01:40 PM`, and the label may be qualified (`PRACTICE GREENS:`) |
| Cells | unquoted, padded to the column count | quoted, ragged rows |
| Metric columns | 19 | 20 — adds `FTP` (face-to-path) before `FTT` |
| Score column | `SHOT SCORE`, or `EXPECTED DIST.` in some files | `SHOT SCORE` |

`FTP` is stored in `shots.face_to_path_deg` and is `NULL` for legacy files, which never
recorded it.

## Loading data

Export a session from SkyTrak and drop the CSV in any folder listed in `SWING_DATA_DIRS`.

```powershell
# Scan every configured folder (already-loaded files are skipped by hash):
python -m src.ingest

# See which folders and files would be scanned, without touching the database:
python -m src.ingest --list

# ...or ingest specific folders and/or files, overriding the configured list:
python -m src.ingest "C:/Users/me/my_docs/swing_stats"
python -m src.ingest "C:/path/to/activity-1788786050.csv"

# Apply schema/migrations/seed first, then ingest, in one go:
python -m src.ingest --init
```

Output is grouped by folder, and each file reports `OK` (inserted), `SKIP` (already
imported), or `ERROR`. Re-running is always safe; duplicates are detected by the file's
SHA-256 hash, not its name — so the same session exported into both folders loads once. A
folder that isn't there (an un-synced OneDrive, an unplugged drive) is reported as a single
error and the remaining folders are still scanned.

## Analyzing data

```powershell
# Full report (all clubs) + dispersion chart, written to reports/:
python -m src.analyze

# Limit to specific clubs:
python -m src.analyze --club 7I PW D

# Use carry instead of total distance on the chart's downrange axis:
python -m src.analyze --distance carry

# Text report only, no chart:
python -m src.analyze --no-chart
```

Outputs (under `reports/`, git-ignored as they contain personal data):

- `club_descriptive_stats.txt` — per-club mean and sample std-dev for every shot metric
  (carry, total, smash, club/ball speed, spin, etc.) plus a left/right side tendency.
- `shot_dispersion.html` — interactive scatter of offline vs. downrange distance, one color
  per club, with mean markers and 1σ/2σ covariance ellipses. Open it in a browser; click a
  club in the legend to toggle it.

Useful flags: `--out PATH`, `--chart-out PATH`, `--no-ellipses`. Run
`python -m src.analyze -h` for the full list.

## Publishing to Neon (cloud)

Mirror your local data up to a [Neon](https://neon.tech/) Postgres database so it can be
reached from anywhere. Set `NEON_DB_URL` in `.env` first (see `.env.example`), then:

```powershell
# First time (and after any new sql/ migration): create/update the schema
# and seed the club list on Neon
python -m src.publish --init

# Publish every local session not yet on Neon:
python -m src.publish

# Re-publish everything, even already-published sessions:
python -m src.publish --all
```

Publishing is **idempotent** — it only sends sessions whose local `published_at` is null, and
a session already on Neon (matched by `file_hash`) is skipped, not duplicated. Each session
is copied in its own transaction, and the local row is stamped `published_at` only after Neon
accepts it. Re-running is always safe. Typical flow after a range session:

```powershell
python -m src.ingest      # load new CSVs into local Postgres
python -m src.publish     # push the new sessions up to Neon
```

## Dashboard

Launch the Streamlit dashboard from the project root:

```powershell
streamlit run app/Home.py
```

It opens at <http://localhost:8501> with three pages:

- **Overview** — KPI summary plus the interactive dispersion chart (toggle total/carry
  distance and the 1σ/2σ ellipses), filterable by session and club.
- **Club Stats** — per-club averages table, a single-club mean/std detail with side tendency,
  and the full text report as a download.
- **Sessions** — the list of ingested sessions (with a "published to Neon" flag) and a
  per-club trend of any metric across sessions over time.

A **Data source** picker in the sidebar switches between the local Postgres and the Neon
cloud mirror (only targets that are configured are offered). The app reuses the same
connection settings as the CLI — local `DB_*` keys plus `NEON_DB_URL`.

When deploying to [Streamlit Community Cloud](https://streamlit.io/cloud), the local database
isn't reachable, so configure only `NEON_DB_URL` (and `DB_SCHEMA` if non-default) in the app's
**Secrets** — see `.streamlit/secrets.toml.example`. `src/config.py` reads Streamlit secrets
automatically when running under Streamlit, falling back to `.env` locally.

## Running tests

```powershell
python -m pytest -q
```

## Project structure

```
src/
  config.py    # env/secrets loading, DB URLs, schema name
  db.py        # SQLAlchemy engines (local + Neon)
  parser.py    # SkyTrak ShotsHistory CSV parser
  ingest.py    # CSV → Postgres loader (CLI)
  analyze.py   # per-club stats + dispersion chart (CLI)
  publish.py   # local → Neon cloud publish (CLI)
app/
  Home.py              # dashboard entry — Overview page (KPIs + dispersion)
  data.py              # shared data access (source picker, cached loaders)
  pages/
    1_Club_Stats.py    # per-club averages, detail, report download
    2_Sessions.py      # session list + per-club trends
sql/
  001_schema.sql              # clubs / sessions / shots tables
  002_seed_clubs.sql          # canonical club list
  003_add_face_to_path.sql    # FTP column added by the current export format
tests/         # parser tests + fixtures
reports/       # generated analysis output (git-ignored)
```

## Roadmap

- Deploy the dashboard to Streamlit Community Cloud, reading from Neon.
