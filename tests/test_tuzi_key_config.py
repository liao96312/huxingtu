from pathlib import Path

import pytest

from scripts.generate_tuzi_panorama import load_api_key


def test_tuzi_key_prefers_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TUZI_API_KEY", "env_" + "x" * 40)
    fallback = tmp_path / "key.txt"
    fallback.write_text("file_" + "y" * 40, encoding="utf-8")

    assert load_api_key(fallback).startswith("env_")


def test_tuzi_key_rejects_missing_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TUZI_API_KEY", raising=False)

    with pytest.raises(SystemExit, match="TUZI_API_KEY"):
        load_api_key()
