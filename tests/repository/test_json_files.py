"""Every JSON file we commit must be valid JSON (a typo would only show up at deploy time otherwise)."""
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
JSON_FILES = sorted((ROOT / "infra").rglob("*.json")) + [ROOT / "config" / "settings.example.json"]


@pytest.mark.parametrize("path", JSON_FILES, ids=lambda p: p.relative_to(ROOT).as_posix())
def test_json_file_is_valid(path):
    assert isinstance(json.loads(path.read_text(encoding="utf-8")), (dict, list))
