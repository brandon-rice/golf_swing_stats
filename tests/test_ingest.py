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
ACTIVITY_FIXTURE = Path(__file__).parent / "fixtures" / "activity_sample.csv"


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

    hashes = [parse_file(f).session.file_hash for f in (FIXTURE, ACTIVITY_FIXTURE)]
    with eng.begin() as conn:
        for file_hash in hashes:
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


def test_ingest_dirs_scans_every_folder(engine, tmp_path):
    """Both export generations load in one pass, each from its own folder."""
    old_dir, new_dir = tmp_path / "old", tmp_path / "new"
    old_dir.mkdir()
    new_dir.mkdir()
    (old_dir / FIXTURE.name).write_bytes(FIXTURE.read_bytes())
    (new_dir / ACTIVITY_FIXTURE.name).write_bytes(ACTIVITY_FIXTURE.read_bytes())

    results = ingest.ingest_dirs([old_dir, new_dir], engine)
    assert [r.status for r in results] == ["inserted", "inserted"]
    assert {r.path.name for r in results} == {FIXTURE.name, ACTIVITY_FIXTURE.name}


def test_ingest_dirs_reports_missing_folder_and_keeps_going(engine, tmp_path):
    """A folder that isn't there (unmounted drive, un-synced OneDrive) is one
    error, not an abort — the folders that do exist still load."""
    good = tmp_path / "good"
    good.mkdir()
    (good / FIXTURE.name).write_bytes(FIXTURE.read_bytes())

    results = ingest.ingest_dirs([tmp_path / "nope", good], engine)
    assert results[0].status == "error"
    assert "folder not found" in (results[0].message or "")
    assert results[1].status == "inserted"


def test_new_format_face_to_path_reaches_the_database(engine):
    result = ingest.ingest_file(ACTIVITY_FIXTURE, engine)
    assert result.status == "inserted"
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT path_deg, face_to_path_deg, face_to_target_deg FROM shots "
                "WHERE session_id = :s AND club_code = '9I' AND shot_number = 1"
            ),
            {"s": result.session_id},
        ).first()
    assert [float(v) for v in row] == [5.4, 2.9, 8.3]
