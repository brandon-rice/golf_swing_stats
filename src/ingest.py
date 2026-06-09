"""Load parsed SkyTrak exports into the local Postgres database.

Responsibilities:
- Apply schema + club seed (idempotent) on demand.
- Ingest a single file or a directory of CSVs.
- Deduplicate by ``sessions.file_hash`` so re-running is safe.
- Auto-register any club code we see that isn't already seeded, so a new
  club abbreviation never hard-fails the FK on ``shots.club_code``.

Each file is ingested in its own transaction: either the session row and all
of its shot rows land, or none of them do.
"""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import config, db
from .parser import ParsedFile, parse_file

SQL_DIR = config.PROJECT_ROOT / "sql"
SCHEMA_FILES = ("001_schema.sql", "002_seed_clubs.sql")


@dataclasses.dataclass
class IngestResult:
    path: Path
    status: str  # "inserted" | "skipped_duplicate" | "error"
    session_id: int | None = None
    shots_inserted: int = 0
    message: str | None = None


def init_schema(engine: Engine) -> None:
    """Create the schema, then apply table + seed files. Safe to run repeatedly."""
    schema = config.db_schema()
    with engine.begin() as conn:
        conn.exec_driver_sql(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
        for name in SCHEMA_FILES:
            sql = (SQL_DIR / name).read_text(encoding="utf-8")
            conn.exec_driver_sql(sql)


def _existing_session_id(conn, file_hash: str) -> int | None:
    row = conn.execute(
        text("SELECT session_id FROM sessions WHERE file_hash = :h"),
        {"h": file_hash},
    ).first()
    return row[0] if row else None


def _ensure_clubs(conn, club_codes: list[str]) -> None:
    """Insert placeholder rows for any club code not already in `clubs`."""
    for code in club_codes:
        conn.execute(
            text(
                "INSERT INTO clubs (club_code, club_name, sort_order) "
                "VALUES (:code, :name, 999) ON CONFLICT (club_code) DO NOTHING"
            ),
            {"code": code, "name": code},
        )


def _insert_session(conn, parsed: ParsedFile) -> int:
    meta = parsed.session
    row = conn.execute(
        text(
            "INSERT INTO sessions (session_ts, player_name, source_file, file_hash, notes) "
            "VALUES (:ts, :player, :src, :hash, :notes) RETURNING session_id"
        ),
        {
            "ts": meta.session_ts,
            "player": meta.player_name,
            "src": meta.source_file,
            "hash": meta.file_hash,
            "notes": meta.notes,
        },
    ).first()
    return row[0]


def _insert_shots(conn, session_id: int, parsed: ParsedFile) -> int:
    cols = db.SHOT_INSERT_COLS
    placeholders = ", ".join(f":{c}" for c in cols)
    stmt = text(
        f"INSERT INTO shots ({', '.join(cols)}) VALUES ({placeholders})"
    )
    params = []
    for shot in parsed.shots:
        d = dataclasses.asdict(shot)
        d["session_id"] = session_id
        params.append({c: d[c] for c in cols})
    if params:
        conn.execute(stmt, params)
    return len(params)


def ingest_file(path: str | Path, engine: Engine) -> IngestResult:
    path = Path(path)
    try:
        parsed = parse_file(path)
    except Exception as ex:  # noqa: BLE001 - surface parse errors per-file
        return IngestResult(path, "error", message=f"parse: {ex}")

    try:
        with engine.begin() as conn:
            existing = _existing_session_id(conn, parsed.session.file_hash)
            if existing is not None:
                return IngestResult(
                    path, "skipped_duplicate", session_id=existing,
                    message="file_hash already imported",
                )
            _ensure_clubs(conn, parsed.session.clubs_seen)
            session_id = _insert_session(conn, parsed)
            n = _insert_shots(conn, session_id, parsed)
        return IngestResult(path, "inserted", session_id=session_id, shots_inserted=n)
    except Exception as ex:  # noqa: BLE001 - surface DB errors per-file
        return IngestResult(path, "error", message=f"db: {ex}")


def ingest_dir(directory: str | Path, engine: Engine) -> list[IngestResult]:
    directory = Path(directory)
    files = sorted(directory.glob("*.csv"))
    return [ingest_file(p, engine) for p in files]


def _format(result: IngestResult) -> str:
    tag = {
        "inserted": "OK   ",
        "skipped_duplicate": "SKIP ",
        "error": "ERROR",
    }.get(result.status, "?    ")
    detail = ""
    if result.status == "inserted":
        detail = f"session {result.session_id}, {result.shots_inserted} shots"
    elif result.status == "skipped_duplicate":
        detail = f"session {result.session_id} (already imported)"
    elif result.status == "error":
        detail = result.message or "unknown error"
    return f"{tag} {result.path.name:40} {detail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Ingest SkyTrak CSV exports into local Postgres.")
    ap.add_argument(
        "path", nargs="?", default=None,
        help="CSV file or directory (default: SWING_DATA_DIR from config).",
    )
    ap.add_argument("--init", action="store_true", help="Apply schema + seed before ingesting.")
    args = ap.parse_args(argv)

    engine = db.local_engine()
    if args.init:
        init_schema(engine)
        print("schema applied")

    target = Path(args.path) if args.path else config.swing_data_dir()
    if target.is_dir():
        results = ingest_dir(target, engine)
    else:
        results = [ingest_file(target, engine)]

    for r in results:
        print(_format(r))

    inserted = sum(1 for r in results if r.status == "inserted")
    shots = sum(r.shots_inserted for r in results)
    errors = sum(1 for r in results if r.status == "error")
    print(f"\n{inserted} session(s) inserted, {shots} shot(s), {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
