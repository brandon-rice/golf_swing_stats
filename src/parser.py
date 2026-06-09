"""Parse SkyTrak ShotsHistory CSV exports.

Format notes:
- Lines 1-3 are a free-form header: `PRACTICE: <date> <time>`, `PLAYER: <name>`, blank.
- Then a two-row column header (names, units).
- Each club's shots are introduced by a delimiter row whose first cell is the club
  code and remaining cells are blank, followed by N shot rows (col 0 = integer),
  then a row whose first cell is `AVG` (to skip), then a blank or another club.
- A trailing `NOTES` row may precede free-form note text.
- Files may contain one or many club blocks; we never trust the filename.
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


SHOT_HEADER_TOKEN = "SHOT"
AVG_TOKEN = "AVG"
NOTES_TOKEN = "NOTES"
PRACTICE_TOKEN = "PRACTICE:"
PLAYER_TOKEN = "PLAYER:"

# A canonical club code matches the seeded `clubs.club_code` values: 1-3
# uppercase letters/digits (e.g. D, 7I, GW, 3W).
CLUB_CODE_RE = re.compile(r"^[A-Z0-9]{1,3}$")

# SkyTrak may label a club block with either its short code (PW, GW, 7W) or its
# full name (7 IRON, 7 WOOD, PITCHING WEDGE). We normalize full names back to the
# canonical seed code so the same club never splits into two club_codes.
_CLUB_NAME_TO_CODE = {
    "DRIVER": "D",
    "PITCHING WEDGE": "PW",
    "GAP WEDGE": "GW",
    "SAND WEDGE": "SW",
    "LOB WEDGE": "LW",
    "PUTTER": "P",
}
_NUMBERED_CLUB_SUFFIX = {"IRON": "I", "WOOD": "W", "HYBRID": "H"}
_NUMBERED_CLUB_RE = re.compile(r"^(\d{1,2})\s+(IRON|WOOD|HYBRID)$")


def _normalize_club_code(label: str) -> str:
    """Map a club delimiter label to the canonical seeded ``club_code``.

    Accepts codes already in canonical form (``PW``, ``7W``, ``GW``) and full
    SkyTrak names (``7 IRON``, ``7 WOOD``, ``PITCHING WEDGE``). Unrecognized
    labels are returned cleaned-but-as-is, so ingest auto-registers them rather
    than hard-failing.
    """
    s = " ".join(label.split()).upper()  # collapse internal/trailing whitespace
    if CLUB_CODE_RE.match(s):
        return s
    if s in _CLUB_NAME_TO_CODE:
        return _CLUB_NAME_TO_CODE[s]
    m = _NUMBERED_CLUB_RE.match(s)
    if m:
        return m.group(1) + _NUMBERED_CLUB_SUFFIX[m.group(2)]
    return s

# Tolerated date formats coming out of SkyTrak.
_DATE_FORMATS = (
    "%m/%d/%Y %I:%M %p",
    "%m/%d/%Y %H:%M",
    "%m/%d/%Y",
)


@dataclass
class ShotRecord:
    club_code: str
    shot_number: int
    hand: str | None
    shot_score: int | None
    ball_speed_mph: float | None
    launch_deg: float | None
    back_spin_rpm: int | None
    side_spin_rpm: int | None
    side_angle_deg: float | None
    offline_yd: float | None
    carry_yd: float | None
    roll_yd: float | None
    total_yd: float | None
    flight_sec: float | None
    descent_deg: float | None
    height_yd: float | None
    club_speed_mph: float | None
    smash_factor: float | None
    path_deg: float | None
    face_to_target_deg: float | None


@dataclass
class SessionMeta:
    session_ts: datetime
    player_name: str | None
    source_file: str
    file_hash: str
    notes: str | None = None
    clubs_seen: list[str] = field(default_factory=list)


@dataclass
class ParsedFile:
    session: SessionMeta
    shots: list[ShotRecord]


def _to_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return int(float(s))
    except ValueError:
        return None


def _to_float(s: str) -> float | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_practice_date(value: str) -> datetime:
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized PRACTICE date format: {value!r}")


def _row_is_blank(row: list[str]) -> bool:
    return all((c or "").strip() == "" for c in row)


def _is_club_delimiter(row: list[str]) -> bool:
    """A club delimiter row has a non-empty, non-numeric first cell and all other
    cells blank. Shot rows (numeric col 0) and AVG/NOTES rows (data in later
    columns, or matched by their own tokens) are handled by the caller first.
    """
    if not row:
        return False
    first = (row[0] or "").strip()
    if not first or _to_int(first) is not None:
        return False
    return all((c or "").strip() == "" for c in row[1:])


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def parse_file(path: str | Path) -> ParsedFile:
    path = Path(path)
    file_hash = _hash_file(path)

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))

    session_ts: datetime | None = None
    player_name: str | None = None
    header_idx: int | None = None

    for i, row in enumerate(rows[:10]):
        joined = ",".join(row)
        if PRACTICE_TOKEN in joined:
            after = joined.split(PRACTICE_TOKEN, 1)[1].strip().strip(",").strip()
            session_ts = _parse_practice_date(after)
        elif PLAYER_TOKEN in joined:
            after = joined.split(PLAYER_TOKEN, 1)[1].strip().strip(",").strip()
            player_name = after or None
        if row and row[0].strip().upper() == SHOT_HEADER_TOKEN:
            header_idx = i
            break

    if session_ts is None:
        raise ValueError(f"{path.name}: missing PRACTICE date header")
    if header_idx is None:
        raise ValueError(f"{path.name}: missing SHOT column header row")

    data_start = header_idx + 2

    shots: list[ShotRecord] = []
    clubs_seen: list[str] = []
    notes_parts: list[str] = []
    current_club: str | None = None
    in_notes = False

    for row in rows[data_start:]:
        if in_notes:
            text = ",".join((c or "").strip() for c in row).strip(",").strip()
            if text:
                notes_parts.append(text)
            continue

        if _row_is_blank(row):
            continue

        first = (row[0] or "").strip().upper()

        if first == NOTES_TOKEN:
            in_notes = True
            continue

        if first == AVG_TOKEN:
            continue

        if _is_club_delimiter(row):
            current_club = _normalize_club_code(first)
            if current_club not in clubs_seen:
                clubs_seen.append(current_club)
            continue

        shot_num = _to_int(first)
        if shot_num is None:
            continue
        if current_club is None:
            raise ValueError(
                f"{path.name}: shot row #{shot_num} found before any club delimiter"
            )

        shots.append(
            ShotRecord(
                club_code=current_club,
                shot_number=shot_num,
                hand=(row[1].strip() if len(row) > 1 else None) or None,
                shot_score=_to_int(row[2]) if len(row) > 2 else None,
                ball_speed_mph=_to_float(row[3]) if len(row) > 3 else None,
                launch_deg=_to_float(row[4]) if len(row) > 4 else None,
                back_spin_rpm=_to_int(row[5]) if len(row) > 5 else None,
                side_spin_rpm=_to_int(row[6]) if len(row) > 6 else None,
                side_angle_deg=_to_float(row[7]) if len(row) > 7 else None,
                offline_yd=_to_float(row[8]) if len(row) > 8 else None,
                carry_yd=_to_float(row[9]) if len(row) > 9 else None,
                roll_yd=_to_float(row[10]) if len(row) > 10 else None,
                total_yd=_to_float(row[11]) if len(row) > 11 else None,
                flight_sec=_to_float(row[12]) if len(row) > 12 else None,
                descent_deg=_to_float(row[13]) if len(row) > 13 else None,
                height_yd=_to_float(row[14]) if len(row) > 14 else None,
                club_speed_mph=_to_float(row[15]) if len(row) > 15 else None,
                smash_factor=_to_float(row[16]) if len(row) > 16 else None,
                path_deg=_to_float(row[17]) if len(row) > 17 else None,
                face_to_target_deg=_to_float(row[18]) if len(row) > 18 else None,
            )
        )

    session = SessionMeta(
        session_ts=session_ts,
        player_name=player_name,
        source_file=path.name,
        file_hash=file_hash,
        notes="\n".join(notes_parts) or None,
        clubs_seen=clubs_seen,
    )
    return ParsedFile(session=session, shots=shots)
