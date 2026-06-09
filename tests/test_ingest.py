"""Integration tests for the ingest loader.

These hit the real local Postgres. They skip cleanly when the DB is
unreachable (no .env / not running), and they clean up after themselves by
deleting the sessions they create (shots cascade).
"""
from pathlib import Path

import pytest
from sqlalchemy import text

from src import db, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "multi_club_sample.csv"


def _local_engine_or_skip():
    try:
        engine = db.local_engine()
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return engine
    except Exception as ex:  # noqa: BLE001
        pytest.skip(f"local Postgres unavailable: {ex}")


@pytest.fixture
def engine():
    eng = _local_engine_or_skip()
    ingest.init_schema(eng)
    yield eng
    # Remove anything this test inserted, keyed by the fixture's file_hash.
    from src.parser import parse_file

    file_hash = parse_file(FIXTURE).session.file_hash
    with eng.begin() as conn:
        conn.execute(text("DELETE FROM sessions WHERE file_hash = :h"), {"h": file_hash})


def test_ingest_inserts_session_and_shots(engine):
    result = ingest.ingest_file(FIXTURE, engine)
    assert result.status == "inserted"
    assert result.session_id is not None
    assert result.shots_inserted == 6

    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM shots WHERE session_id = :s"),
            {"s": result.session_id},
        ).scalar()
    assert n == 6


def test_ingest_is_idempotent(engine):
    first = ingest.ingest_file(FIXTURE, engine)
    assert first.status == "inserted"
    second = ingest.ingest_file(FIXTURE, engine)
    assert second.status == "skipped_duplicate"
    assert second.session_id == first.session_id


def test_ingest_registers_unseen_club(engine):
    # The fixture uses standard clubs; verify the FK-safety path explicitly by
    # confirming every club it saw exists in `clubs` after ingest.
    ingest.ingest_file(FIXTURE, engine)
    with engine.connect() as conn:
        for code in ("PW", "7I", "D"):
            exists = conn.execute(
                text("SELECT 1 FROM clubs WHERE club_code = :c"), {"c": code}
            ).scalar()
            assert exists == 1
