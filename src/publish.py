"""Publish local sessions to the Neon (cloud) Postgres database.

Flow:
- Find local sessions still marked unpublished (``published_at IS NULL``).
- For each, copy the session row and all of its shots to Neon. Because
  ``session_id`` is a serial, Neon assigns its own id; shots are re-pointed at
  that new id. The unique ``file_hash`` makes the copy idempotent — a session
  already on Neon is detected and skipped, not duplicated.
- After Neon commits a session, stamp ``published_at = NOW()`` on the local row.

Each session is published in its own Neon transaction, and the local row is
only stamped once Neon has durably accepted it. Re-running is always safe.

CLI:  python -m src.publish [--init] [--all]
  --init  Apply schema + club seed to Neon first (idempotent).
  --all   Re-publish every local session, even ones already stamped.
"""
from __future__ import annotations

import argparse
import dataclasses
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Engine

from . import config, db

SQL_DIR = config.PROJECT_ROOT / "sql"
SCHEMA_FILES = (
    "001_schema.sql",
    "002_seed_clubs.sql",
    "003_add_face_to_path.sql",
    "004_merge_loft_labeled_wedges.sql",
)


@dataclasses.dataclass
class PublishResult:
    session_id: int
    source_file: str
    status: str  # "published" | "already_on_neon" | "error"
    shots_published: int = 0
    message: str | None = None


def init_neon_schema(engine: Engine) -> None:
    """Apply table + seed files to Neon's public schema. Safe to run repeatedly."""
    with engine.begin() as conn:
        for name in SCHEMA_FILES:
            sql = (SQL_DIR / name).read_text(encoding="utf-8")
            conn.exec_driver_sql(sql)


def _unpublished_sessions(local: Engine, include_all: bool) -> list[dict]:
    where = "" if include_all else "WHERE published_at IS NULL"
    sql = text(
        f"SELECT session_id, session_ts, player_name, source_file, file_hash, notes "
        f"FROM sessions {where} ORDER BY session_ts, session_id"
    )
    with local.connect() as conn:
        return [dict(r) for r in conn.execute(sql).mappings()]


def _sync_clubs(local: Engine, neon: Engine) -> None:
    """Copy the local clubs table to Neon (FK safety for any non-seeded codes)."""
    with local.connect() as conn:
        clubs = [dict(r) for r in conn.execute(
            text("SELECT club_code, club_name, club_type, sort_order FROM clubs")
        ).mappings()]
    if not clubs:
        return
    with neon.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO clubs (club_code, club_name, club_type, sort_order) "
                "VALUES (:club_code, :club_name, :club_type, :sort_order) "
                "ON CONFLICT (club_code) DO NOTHING"
            ),
            clubs,
        )


def _local_shots(local: Engine, session_id: int) -> list[dict]:
    cols = ", ".join(db.SHOT_INSERT_COLS)
    with local.connect() as conn:
        return [dict(r) for r in conn.execute(
            text(f"SELECT {cols} FROM shots WHERE session_id = :sid ORDER BY shot_number"),
            {"sid": session_id},
        ).mappings()]


def _publish_one(local: Engine, neon: Engine, sess: dict) -> PublishResult:
    sid = sess["session_id"]
    src = sess["source_file"]
    try:
        with neon.begin() as conn:
            existing = conn.execute(
                text("SELECT session_id FROM sessions WHERE file_hash = :h"),
                {"h": sess["file_hash"]},
            ).first()
            if existing is not None:
                return PublishResult(sid, src, "already_on_neon",
                                     message="file_hash already on Neon")

            new_id = conn.execute(
                text(
                    "INSERT INTO sessions "
                    "(session_ts, player_name, source_file, file_hash, notes) "
                    "VALUES (:session_ts, :player_name, :source_file, :file_hash, :notes) "
                    "RETURNING session_id"
                ),
                sess,
            ).scalar_one()

            shots = _local_shots(local, sid)
            for s in shots:
                s["session_id"] = new_id
            if shots:
                placeholders = ", ".join(f":{c}" for c in db.SHOT_INSERT_COLS)
                conn.execute(
                    text(f"INSERT INTO shots ({', '.join(db.SHOT_INSERT_COLS)}) "
                         f"VALUES ({placeholders})"),
                    shots,
                )
    except Exception as ex:  # noqa: BLE001 - surface per-session, keep going
        return PublishResult(sid, src, "error", message=str(ex))

    # Neon has durably accepted the session; stamp the local row.
    with local.begin() as conn:
        conn.execute(
            text("UPDATE sessions SET published_at = NOW() WHERE session_id = :sid"),
            {"sid": sid},
        )
    return PublishResult(sid, src, "published", shots_published=len(shots))


def publish(local: Engine, neon: Engine, include_all: bool = False) -> list[PublishResult]:
    sessions = _unpublished_sessions(local, include_all)
    if not sessions:
        return []
    _sync_clubs(local, neon)
    return [_publish_one(local, neon, s) for s in sessions]


def _format(r: PublishResult) -> str:
    tag = {
        "published": "OK   ",
        "already_on_neon": "SKIP ",
        "error": "ERROR",
    }.get(r.status, "?    ")
    detail = ""
    if r.status == "published":
        detail = f"{r.shots_published} shots"
    elif r.status == "already_on_neon":
        detail = "already on Neon (local stamped on a prior run)"
    elif r.status == "error":
        detail = r.message or "unknown error"
    return f"{tag} session {r.session_id:<4} {r.source_file:40} {detail}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Publish local sessions to Neon Postgres.")
    ap.add_argument("--init", action="store_true", help="Apply schema + seed to Neon first.")
    ap.add_argument("--all", action="store_true", help="Re-publish all sessions, not just unpublished.")
    args = ap.parse_args(argv)

    local = db.local_engine()
    neon = db.neon_engine()

    if args.init:
        init_neon_schema(neon)
        print("Neon schema applied")

    results = publish(local, neon, include_all=args.all)
    if not results:
        print("Nothing to publish — all local sessions are already published.")
        return 0

    for r in results:
        print(_format(r))

    published = sum(1 for r in results if r.status == "published")
    shots = sum(r.shots_published for r in results)
    errors = sum(1 for r in results if r.status == "error")
    print(f"\n{published} session(s) published, {shots} shot(s), {errors} error(s).")
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
