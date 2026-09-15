"""Parse SkyTrak shot-history CSV exports.

Two export generations are supported; both are detected from the file itself,
never from the filename or its folder.

*Legacy* (``Export_ShotsHistory_*.csv``) — unquoted, every row padded to the
column count, header ``PRACTICE: 6/8/2026 12:32 PM``, 19 metric columns.

*Current* (``activity-*.csv``) — quoted, ragged rows, header
``PRACTICE: 07/09/2026 • 01:40 PM`` (a bullet between date and time, and the
label may be qualified, e.g. ``PRACTICE GREENS:``), and one extra metric column
(``FTP``, face-to-path) wedged in before ``FTT``.

Shared structure:
- A free-form header: `PRACTICE...: <date> <time>`, `PLAYER: <name>`, blank.
- Then a two-row column header (names, then units). Metric columns are located
  by that header pair rather than by position, so an inserted, dropped, or
  renamed column shifts nothing.
- Each club's shots are introduced by a delimiter row whose first cell is the club
  code and remaining cells are blank, followed by N shot rows (col 0 = integer),
  then a row whose first cell is `AVG` (to skip), then a blank or another club.
- A trailing `NOTES` row may precede free-form note text.
- Files may contain one or many club blocks.
"""
from __future__ import annotations

import csv
import dataclasses
import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


SHOT_HEADER_TOKEN = "SHOT"
AVG_TOKEN = "AVG"
NOTES_TOKEN = "NOTES"
PLAYER_TOKEN = "PLAYER:"

# The session line is `PRACTICE: <when>`, but newer exports qualify the label
# ("PRACTICE GREENS: ..."), so match the word plus anything up to the colon.
PRACTICE_RE = re.compile(r"PRACTICE[^:,]*:")

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

# SkyTrak sometimes labels a wedge by its loft ("56°") rather than a code, which
# would otherwise auto-register as a club of its own. Lofts are mapped to the
# wedge that carries them in this bag: the 60 is the lob wedge (chip club), the
# 56 the sand wedge. No canonical club code is purely numeric, so the degree
# sign is optional and a bare number is still unambiguously a loft.
_LOFT_TO_CODE = {
    46: "PW", 47: "PW", 48: "PW",
    50: "GW", 52: "GW",
    54: "SW", 56: "SW",
    58: "LW", 60: "LW",
}
_LOFT_RE = re.compile(r"^(\d{2})\s*(?:°|DEG(?:REES?)?)?$")


def _normalize_club_code(label: str) -> str:
    """Map a club delimiter label to the canonical seeded ``club_code``.

    Accepts codes already in canonical form (``PW``, ``7W``, ``GW``), full
    SkyTrak names (``7 IRON``, ``7 WOOD``, ``PITCHING WEDGE``), and wedge loft
    labels (``56°``, ``60``). Unrecognized labels are returned cleaned-but-as-is,
    so ingest auto-registers them rather than hard-failing.
    """
    s = " ".join(label.split()).upper()  # collapse internal/trailing whitespace
    loft = _LOFT_RE.match(s)
    if loft and int(loft.group(1)) in _LOFT_TO_CODE:
        return _LOFT_TO_CODE[int(loft.group(1))]
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

# Metric columns are located by their (name, unit) pair from the two header
# rows, not by position. `SIDE` appears twice — spin and angle — and is told
# apart only by its unit, which is why the unit row is part of the key.
_COLUMN_MAP: dict[tuple[str, str], str] = {
    ("HAND", "L/R"): "hand",
    ("SHOT SCORE", "SCORE"): "shot_score",
    # Some legacy exports relabel the very same score column as a distance.
    ("EXPECTED DIST.", "SCORE"): "shot_score",
    ("BALL SPEED", "MPH"): "ball_speed_mph",
    ("LAUNCH", "DEG"): "launch_deg",
    ("BACK", "RPM"): "back_spin_rpm",
    ("SIDE", "RPM"): "side_spin_rpm",
    ("SIDE", "DEG"): "side_angle_deg",
    ("OFFLINE", "YD"): "offline_yd",
    ("CARRY", "YD"): "carry_yd",
    ("ROLL", "YD"): "roll_yd",
    ("TOTAL", "YD"): "total_yd",
    ("FLIGHT", "SEC"): "flight_sec",
    ("DSCNT", "DEG"): "descent_deg",
    ("HEIGHT", "YD"): "height_yd",
    ("CLUB SPEED", "MPH"): "club_speed_mph",
    ("SMASH", "FACTOR"): "smash_factor",
    ("PATH", "DEG"): "path_deg",
    ("FTP", "DEG"): "face_to_path_deg",   # face-to-path; current exports only
    ("FTT", "DEG"): "face_to_target_deg",
}

# Fields read as integers; everything else in _COLUMN_MAP is a float, except
# `hand`, which stays a string.
_INT_FIELDS = frozenset({"shot_score", "back_spin_rpm", "side_spin_rpm"})


def _column_index(names: list[str], units: list[str]) -> dict[str, int]:
    """Map each known metric field to its column index in this file.

    Columns we don't recognize are ignored rather than fatal, so a future
    SkyTrak addition costs us that one metric instead of the whole file.
    """
    index: dict[str, int] = {}
    for i, raw_name in enumerate(names):
        name = " ".join((raw_name or "").split()).upper()
        unit = " ".join((units[i] if i < len(units) else "").split()).upper()
        field_name = _COLUMN_MAP.get((name, unit))
        if field_name is not None and field_name not in index:
            index[field_name] = i
    return index


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
    face_to_path_deg: float | None
    face_to_target_deg: float | None


# Every ShotRecord field filled from a mapped column (i.e. all but the two the
# block structure supplies: club_code and shot_number).
_METRIC_FIELDS: tuple[str, ...] = tuple(
    f.name for f in dataclasses.fields(ShotRecord)
    if f.name not in ("club_code", "shot_number")
)


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
    # Current exports separate date and time with a bullet ("07/09/2026 • 01:40 PM");
    # drop it and collapse the whitespace so one format list covers both generations.
    value = " ".join(value.replace("•", " ").split())
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
        practice = PRACTICE_RE.search(joined)
        if practice:
            after = joined[practice.end():].strip().strip(",").strip()
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

    units_row = rows[header_idx + 1] if len(rows) > header_idx + 1 else []
    columns = _column_index(rows[header_idx], units_row)
    if "carry_yd" not in columns:
        raise ValueError(f"{path.name}: column header has no recognizable CARRY column")

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

        values: dict[str, object] = {}
        for field_name, col in columns.items():
            cell = row[col] if col < len(row) else ""
            if field_name == "hand":
                values[field_name] = (cell or "").strip() or None
            elif field_name in _INT_FIELDS:
                values[field_name] = _to_int(cell)
            else:
                values[field_name] = _to_float(cell)

        shots.append(
            ShotRecord(
                club_code=current_club,
                shot_number=shot_num,
                # Metrics absent from this file's header stay None.
                **{f: values.get(f) for f in _METRIC_FIELDS},
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
