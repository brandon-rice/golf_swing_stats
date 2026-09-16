"""Tests for the data-folder configuration.

The pipeline scans more than one export folder (SkyTrak changed both its export
filename and its column layout mid-stream, and the old exports still live in a
different folder), so the folder list is the piece worth pinning down.
"""
import os

import pytest

from src import config


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("SWING_DATA_DIRS", raising=False)
    monkeypatch.delenv("SWING_DATA_DIR", raising=False)


def test_multiple_dirs_split_on_pathsep(monkeypatch, tmp_path):
    a, b = tmp_path / "old", tmp_path / "new"
    monkeypatch.setenv("SWING_DATA_DIRS", f"{a}{os.pathsep}{b}")
    assert config.swing_data_dirs() == [a, b]


def test_singular_key_still_works(monkeypatch, tmp_path):
    monkeypatch.setenv("SWING_DATA_DIR", str(tmp_path))
    assert config.swing_data_dirs() == [tmp_path]
    assert config.swing_data_dir() == tmp_path


def test_singular_is_appended_to_plural(monkeypatch, tmp_path):
    a, b = tmp_path / "old", tmp_path / "new"
    monkeypatch.setenv("SWING_DATA_DIRS", str(a))
    monkeypatch.setenv("SWING_DATA_DIR", str(b))
    assert config.swing_data_dirs() == [a, b]


def test_duplicate_and_blank_entries_are_dropped(monkeypatch, tmp_path):
    a = tmp_path / "old"
    monkeypatch.setenv("SWING_DATA_DIRS", f"{a}{os.pathsep}{os.pathsep}{a}")
    monkeypatch.setenv("SWING_DATA_DIR", str(a))
    assert config.swing_data_dirs() == [a]


def test_no_folders_configured_raises():
    with pytest.raises(RuntimeError, match="No data folders configured"):
        config.swing_data_dirs()
