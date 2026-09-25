"""Load config/models.yaml from the repository root."""
from __future__ import annotations

from pathlib import Path

import yaml

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_MODELS = _REPO_ROOT / "config" / "models.yaml"


def load_models_config(path: Path | None = None) -> dict:
    cfg_path = path if path is not None else _DEFAULT_MODELS
    if not cfg_path.is_file():
        raise FileNotFoundError(f"models config not found: {cfg_path}")
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"models config must be a mapping: {cfg_path}")
    return data
