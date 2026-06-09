from datetime import datetime
from pathlib import Path

import pytest

from src.parser import _normalize_club_code, parse_file


FIXTURE = Path(__file__).parent / "fixtures" / "multi_club_sample.csv"
REAL_PW = Path("C:/Users/brand/OneDrive/Documents/swing_stats/Export_ShotsHistory_06082026_123337.csv")
REAL_MULTI = Path("C:/Users/brand/OneDrive/Documents/swing_stats/Export_ShotsHistory_06092026_134151.csv")


def test_multi_club_session_meta():
    result = parse_file(FIXTURE)
    assert result.session.session_ts == datetime(2026, 6, 9, 9, 15)
    assert result.session.player_name == "TESTUSER"
    assert result.session.source_file == "multi_club_sample.csv"
    assert len(result.session.file_hash) == 64
    assert result.session.clubs_seen == ["PW", "7I", "D"]
    assert "windy day" in (result.session.notes or "")


def test_multi_club_shot_counts():
    result = parse_file(FIXTURE)
    by_club: dict[str, int] = {}
    for s in result.shots:
        by_club[s.club_code] = by_club.get(s.club_code, 0) + 1
    assert by_club == {"PW": 2, "7I": 3, "D": 1}
    assert len(result.shots) == 6


def test_avg_rows_are_skipped():
    result = parse_file(FIXTURE)
    pw_shot_numbers = [s.shot_number for s in result.shots if s.club_code == "PW"]
    assert pw_shot_numbers == [1, 2]


def test_first_shot_values_round_trip():
    result = parse_file(FIXTURE)
    first = next(s for s in result.shots if s.club_code == "PW" and s.shot_number == 1)
    assert first.hand == "R"
    assert first.ball_speed_mph == 96.0
    assert first.carry_yd == 127.0
    assert first.total_yd == 132.0
    assert first.offline_yd == 5.0
    assert first.smash_factor == 1.3
    assert first.back_spin_rpm == 6000
    assert first.side_spin_rpm == -400


def test_negative_offline_is_left():
    result = parse_file(FIXTURE)
    shot = next(s for s in result.shots if s.club_code == "PW" and s.shot_number == 2)
    assert shot.offline_yd == -14.0


@pytest.mark.parametrize(
    "label, expected",
    [
        ("PW", "PW"),        # already canonical short code
        ("7I", "7I"),
        ("GW ", "GW"),       # trailing space tolerated
        ("7 IRON", "7I"),    # full SkyTrak name -> seed code
        ("7 WOOD", "7W"),
        ("3 HYBRID", "3H"),
        ("DRIVER", "D"),
        ("PITCHING WEDGE", "PW"),
        ("gap wedge", "GW"),  # case-insensitive
    ],
)
def test_normalize_club_code(label, expected):
    assert _normalize_club_code(label) == expected


@pytest.mark.skipif(not REAL_MULTI.exists(), reason="real export file not present")
def test_real_multiword_club_export():
    result = parse_file(REAL_MULTI)
    assert result.session.session_ts == datetime(2026, 6, 9, 13, 41)
    # Full club names normalize to canonical seed codes, preserving order.
    assert result.session.clubs_seen == ["7I", "GW", "7W"]
    by_club: dict[str, int] = {}
    for s in result.shots:
        by_club[s.club_code] = by_club.get(s.club_code, 0) + 1
    assert by_club == {"7I": 21, "GW": 22, "7W": 22}


@pytest.mark.skipif(not REAL_PW.exists(), reason="real export file not present")
def test_real_pw_export():
    result = parse_file(REAL_PW)
    assert result.session.session_ts == datetime(2026, 6, 8, 12, 32)
    assert result.session.player_name == "BRANDON10"
    assert result.session.clubs_seen == ["PW"]
    pw_shots = [s for s in result.shots if s.club_code == "PW"]
    assert len(pw_shots) == 25
    carries = [s.carry_yd for s in pw_shots if s.carry_yd is not None]
    avg_carry = sum(carries) / len(carries)
    assert 128 <= avg_carry <= 130
