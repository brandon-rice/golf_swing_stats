"""Centralized config. Reads from Streamlit secrets when running under Streamlit,
otherwise falls back to .env via python-dotenv, then os.environ."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote_plus

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(PROJECT_ROOT / ".env")


def _from_streamlit(key: str) -> str | None:
    try:
        import streamlit as st
        return st.secrets.get(key)
    except Exception:
        return None


def get(key: str, default: str | None = None) -> str | None:
    return _from_streamlit(key) or os.environ.get(key, default)


DEFAULT_SCHEMA = "golf_swing_stats"


def db_schema() -> str:
    """Postgres schema that holds the golf tables (local target)."""
    return get("DB_SCHEMA") or DEFAULT_SCHEMA


def local_db_url() -> str:
    """Local Postgres URL.

    Prefers an explicit ``LOCAL_DB_URL``; otherwise assembles one from the
    split ``DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD`` keys.
    """
    url = get("LOCAL_DB_URL")
    if url:
        return url

    host = get("DB_HOST")
    name = get("DB_NAME")
    user = get("DB_USER")
    if not (host and name and user):
        raise RuntimeError(
            "Local DB not configured: set LOCAL_DB_URL, or "
            "DB_HOST/DB_NAME/DB_USER (+ DB_PASSWORD/DB_PORT) in .env"
        )
    password = get("DB_PASSWORD") or ""
    port = get("DB_PORT") or "5432"
    auth = f"{quote_plus(user)}:{quote_plus(password)}" if password else quote_plus(user)
    return f"postgresql://{auth}@{host}:{port}/{name}"


def neon_db_url() -> str:
    url = get("NEON_DB_URL")
    if not url:
        raise RuntimeError("NEON_DB_URL not set (check .env or Streamlit secrets)")
    return url


def swing_data_dir() -> Path:
    raw = get("SWING_DATA_DIR")
    if not raw:
        raise RuntimeError("SWING_DATA_DIR not set (check .env)")
    return Path(raw)
