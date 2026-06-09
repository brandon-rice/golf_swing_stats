"""Integration tests for the Neon publish flow.

These hit both the real local Postgres and the real Neon database. They skip
cleanly when either is unreachable (no .env / not running / NEON_DB_URL unset),
and they clean up after themselves by deleting the fixture's session from both
databases (shots cascade), keyed by its file_hash.
"""
from pathlib import Path

import pytest
from sqlalchemy import text

from src import db, ingest, publish
from src.parser import parse_file

FIXTURE = Path(__file__).parent / "fixtures" / "multi_club_sample.csv"


def _engine_or_skip(factory, label):
    try:
        engine = factory()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"{label} unavailable: {ex}")


def _purge(engine, file_hash):
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM sessions WHERE file_hash = :h"), {"h": file_hash})


def _fetch_session(engine, file_hash):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(
                    "SELECT session_id, session_ts, player_name, source_file, "
                    "file_hash, notes FROM sessions WHERE file_hash = :h"
                ),
                {"h": file_hash},
            ).mappings().one()
        )


@pytest.fixture
def engines():
    local = _engine_or_skip(db.local_engine, "local Postgres")
    neon = _engine_or_skip(db.neon_engine, "Neon")
    ingest.init_schema(local)
    publish.init_neon_schema(neon)
    file_hash = parse_file(FIXTURE).session.file_hash
    # Clean slate on both, before and after.
    _purge(local, file_hash)
    _purge(neon, file_hash)
    yield local, neon, file_hash
    _purge(local, file_hash)
    _purge(neon, file_hash)


def test_publish_copies_session_and_shots(engines):
    local, neon, file_hash = engines
    ingest.ingest_file(FIXTURE, local)  # creates an unpublished local session
    sess = _fetch_session(local, file_hash)

    publish._sync_clubs(local, neon)
    result = publish._publish_one(local, neon, sess)
    assert result.status == "published"
    assert result.shots_published == 6

    # Shots landed on Neon under the remapped session_id.
    with neon.connect() as conn:
        n = conn.execute(
            text(
                "SELECT count(*) FROM shots sh "
                "JOIN sessions s ON s.session_id = sh.session_id "
                "WHERE s.file_hash = :h"
            ),
            {"h": file_hash},
        ).scalar()
    assert n == 6

    # Local row is stamped as published.
    with local.connect() as conn:
        published_at = conn.execute(
            text("SELECT published_at FROM sessions WHERE file_hash = :h"),
            {"h": file_hash},
        ).scalar()
    assert published_at is not None


def test_publish_is_idempotent(engines):
    local, neon, file_hash = engines
    ingest.ingest_file(FIXTURE, local)
    sess = _fetch_session(local, file_hash)

    publish._sync_clubs(local, neon)
    first = publish._publish_one(local, neon, sess)
    assert first.status == "published"

    # A second publish of the same session is a no-op detected by file_hash.
    second = publish._publish_one(local, neon, sess)
    assert second.status == "already_on_neon"

    with neon.connect() as conn:
        sessions = conn.execute(
            text("SELECT count(*) FROM sessions WHERE file_hash = :h"),
            {"h": file_hash},
        ).scalar()
    assert sessions == 1
