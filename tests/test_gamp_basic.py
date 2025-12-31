import os
from pathlib import Path
import json
import tempfile
import sys
import importlib.util

import pytest

# Ensure project root is on sys.path so tests can import Gamp
sys.path.insert(0, str(Path(__file__).parents[1]))
from Gamp import get_file_metadata, Config


def test_get_file_metadata_artist_title_parsing():
    title, artist, album = get_file_metadata("/music/Artist Name - Some Title.mp3", 123.0)
    assert artist == "Artist Name"
    assert title == "Some Title"
    assert album == "Unknown Album"


def test_config_save_and_load(tmp_path):
    # Create a temporary config directory
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    cfg = Config()

    # Override paths to use tmp_path
    cfg.config_dir = cfg_dir
    cfg.config_file = cfg_dir / "config.json"
    cfg.playlist_file = cfg_dir / "playlist.json"

    cfg.data["volume"] = 0.42
    saved = cfg.save()
    assert saved is True
    assert cfg.config_file.exists()

    # Load into a fresh Config instance pointing to same directory
    cfg2 = Config()
    cfg2.config_dir = cfg_dir
    cfg2.config_file = cfg_dir / "config.json"
    loaded = cfg2.load()
    assert isinstance(loaded, dict)
    assert abs(loaded.get("volume", 0) - 0.42) < 1e-6
