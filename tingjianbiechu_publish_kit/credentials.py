"""Load project credentials: .env first, then process environment.

Missing .env, or blank values in .env, fall back to os.environ of the same name.
Never prints or logs secret values.
"""
from __future__ import annotations

import os
from pathlib import Path

# Declared project credential names (keep in sync with .env.sample).
CREDENTIAL_NAMES: tuple[str, ...] = (
    "DEEPSEEK_API_KEY",
    "DASHSCOPE_API_KEY",
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_ENV_FILE = _REPO_ROOT / ".env"


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal KEY=VALUE .env file. Comments and blank lines ignored."""
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def load_dotenv_file(path: Path | None = None) -> dict[str, str]:
    """Return key/value pairs from .env if the file exists; otherwise {}."""
    env_path = path if path is not None else _DEFAULT_ENV_FILE
    if not env_path.is_file():
        return {}
    return _parse_dotenv(env_path)


def get_credential(
    name: str,
    *,
    env_file: Path | None = None,
    default: str | None = None,
) -> str | None:
    """Resolve one credential: non-empty .env value, else non-empty os.environ, else default."""
    file_values = load_dotenv_file(env_file)
    if name in file_values:
        file_value = file_values[name].strip()
        if file_value:
            return file_value
    env_value = os.environ.get(name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    return default


def get_credentials(
    names: tuple[str, ...] | None = None,
    *,
    env_file: Path | None = None,
) -> dict[str, str | None]:
    """Resolve several credentials; missing/blank keys map to None."""
    keys = names if names is not None else CREDENTIAL_NAMES
    return {name: get_credential(name, env_file=env_file) for name in keys}
