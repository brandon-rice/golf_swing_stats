CREATE TABLE IF NOT EXISTS clubs (
    club_code   TEXT PRIMARY KEY,
    club_name   TEXT NOT NULL,
    club_type   TEXT,
    sort_order  INT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id   BIGSERIAL PRIMARY KEY,
    session_ts   TIMESTAMP NOT NULL,
    player_name  TEXT,
    source_file  TEXT NOT NULL,
    file_hash    TEXT NOT NULL UNIQUE,
    notes        TEXT,
    imported_at  TIMESTAMPTZ DEFAULT NOW(),
    published_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS shots (
    shot_id            BIGSERIAL PRIMARY KEY,
    session_id         BIGINT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    club_code          TEXT   NOT NULL REFERENCES clubs(club_code),
    shot_number        INT    NOT NULL,
    hand               CHAR(1),
    shot_score         INT,
    ball_speed_mph     NUMERIC(5,1),
    launch_deg         NUMERIC(4,1),
    back_spin_rpm      INT,
    side_spin_rpm      INT,
    side_angle_deg     NUMERIC(4,1),
    offline_yd         NUMERIC(5,1),
    carry_yd           NUMERIC(5,1),
    roll_yd            NUMERIC(5,1),
    total_yd           NUMERIC(5,1),
    flight_sec         NUMERIC(4,1),
    descent_deg        NUMERIC(4,1),
    height_yd          NUMERIC(5,1),
    club_speed_mph     NUMERIC(5,1),
    smash_factor       NUMERIC(4,2),
    path_deg           NUMERIC(4,1),
    face_to_target_deg NUMERIC(4,1),
    UNIQUE (session_id, club_code, shot_number)
);

CREATE INDEX IF NOT EXISTS idx_shots_club_session ON shots (club_code, session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_ts        ON sessions (session_ts);
CREATE INDEX IF NOT EXISTS idx_sessions_unpub     ON sessions (published_at) WHERE published_at IS NULL;
