"""SQLAlchemy engines for the two Postgres targets."""
from __future__ import annotations

from functools import lru_cache

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from . import config


@lru_cache(maxsize=1)
def local_engine() -> Engine:
    # Pin the search_path so every connection reads/writes the golf schema.
    schema = config.db_schema()
    return create_engine(
        config.local_db_url(),
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )


@lru_cache(maxsize=1)
def neon_engine() -> Engine:
    return create_engine(config.neon_db_url(), future=True, pool_pre_ping=True)


SHOT_INSERT_COLS = (
    "session_id",
    "club_code",
    "shot_number",
    "hand",
    "shot_score",
    "ball_speed_mph",
    "launch_deg",
    "back_spin_rpm",
    "side_spin_rpm",
    "side_angle_deg",
    "offline_yd",
    "carry_yd",
    "roll_yd",
    "total_yd",
    "flight_sec",
    "descent_deg",
    "height_yd",
    "club_speed_mph",
    "smash_factor",
    "path_deg",
    "face_to_path_deg",
    "face_to_target_deg",
)
