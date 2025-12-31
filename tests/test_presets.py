from pathlib import Path
import tempfile

from Gamp import PresetManager


def test_preset_manager_loads_embedded_when_missing(tmp_path):
    pm = PresetManager(preset_dir=str(tmp_path / "nope"))
    # Ensure the directory doesn't exist
    if (tmp_path / "nope").exists():
        (tmp_path / "nope").unlink()
    pm.load_presets()
    assert len(pm.available_presets) > 0
    assert isinstance(pm.preset_categories, dict)


def test_categorize_preset_examples():
    pm = PresetManager()
    assert pm.categorize_preset("Wave Form") == "Flowing"
    assert pm.categorize_preset("Star Burst") == "Space"
    assert pm.categorize_preset("Crystalize") == "Crystal"