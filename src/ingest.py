"""Load parsed SkyTrak exports into the local Postgres database.

Responsibilities:
- Apply schema + club seed (idempotent) on demand.
- Ingest a single file, a folder of CSVs, or every folder configured in
  ``SWING_DATA_DIRS`` — SkyTrak's old and new export folders are scanned in
  one pass and either export format is accepted.
- Deduplicate by ``sessions.file_hash`` so re-running is safe.
- Auto-register any club code we see that isn't already seeded, so a new
  club abbreviation never hard-fails the FK on ``shots.club_code``.

Each file is ingested in its own transaction: either the session row and all
of its shot rows land, or none of them do.
"""
from __future__ import annotations

import argparse
import dataclasses
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import config, db
from .parser import ParsedFile, parse_file

SQL_DIR = config.PROJECT_ROOT / "sql"
SCHEMA_FILES = (
    "001_schema.sql",
    "002_seed_clubs.sql",
    "003_add_face_to_path.sql",
    "004_merge_loft_labeled_wedges.sql",
)


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


def csv_files(directory: str | Path) -> list[Path]:
    """CSVs in ``directory``, name-sorted. Both export generations live side by
    side, so we match every ``*.csv`` and let the parser sort out the format."""
    return sorted(Path(directory).glob("*.csv"))


def ingest_dir(directory: str | Path, engine: Engine) -> list[IngestResult]:
    return [ingest_file(p, engine) for p in csv_files(directory)]


def ingest_dirs(directories: Iterable[str | Path], engine: Engine) -> list[IngestResult]:
    """Ingest every CSV across several folders, in the order given.

    A folder that doesn't exist is reported as one error and the remaining
    folders are still scanned — a disconnected OneDrive shouldn't stop the
    local exports from loading. Files duplicated across folders are caught by
    the usual ``file_hash`` check, so overlapping folders are harmless.
    """
    results: list[IngestResult] = []
    for directory in directories:
        directory = Path(directory)
        if not directory.is_dir():
            results.append(IngestResult(directory, "error", message="folder not found"))
            continue
        results.extend(ingest_dir(directory, engine))
    return results


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


def _ingest_target(target: Path, engine: Engine) -> list[IngestResult]:
    """Ingest one CLI target — a folder of CSVs, a single CSV, or a bad path."""
    if target.is_dir():
        files = csv_files(target)
        print(f"\n{target}  ({len(files)} csv file(s))")
        return [ingest_file(f, engine) for f in files]
    print(f"\n{target.parent}")
    if not target.exists():
        return [IngestResult(target, "error", message="path not found")]
    return [ingest_file(target, engine)]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Ingest SkyTrak CSV exports into local Postgres.",
    )
    ap.add_argument(
        "paths", nargs="*",
        help="CSV files and/or folders to scan "
             "(default: every folder in SWING_DATA_DIRS / SWING_DATA_DIR).",
    )
    ap.add_argument("--init", action="store_true", help="Apply schema + seed before ingesting.")
    ap.add_argument(
        "--list", action="store_true", dest="list_only",
        help="List the folders and files that would be scanned, then exit.",
    )
    args = ap.parse_args(argv)

    targets = [Path(p) for p in args.paths] if args.paths else config.swing_data_dirs()

    if args.list_only:
        for target in targets:
            if target.is_dir():
                files = csv_files(target)
                print(f"{target}  ({len(files)} csv file(s))")
                for f in files:
                    print(f"    {f.name}")
            else:
                mark = "" if target.exists() else "   [missing]"
                print(f"{target}{mark}")
        return 0

    engine = db.local_engine()
    if args.init:
        init_schema(engine)
        print("schema applied")

    results: list[IngestResult] = []
    for target in targets:
        batch = _ingest_target(target, engine)
        for r in batch:
            print("  " + _format(r))
        results.extend(batch)

    inserted = sum(1 for r in results if r.status == "inserted")
    skipped = sum(1 for r in results if r.status == "skipped_duplicate")
    shots = sum(r.shots_inserted for r in results)
    errors = sum(1 for r in results if r.status == "error")
    print(
        f"\n{len(targets)} folder(s)/file(s) scanned: {inserted} session(s) inserted, "
        f"{shots} shot(s), {skipped} already loaded, {errors} error(s)."
    )
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
