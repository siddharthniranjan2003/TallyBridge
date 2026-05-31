from __future__ import annotations

import os
from pathlib import Path
from typing import Callable


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def make_env_loader(path: Path) -> Callable[..., str]:
    values = load_env_file(Path(path))

    def env_value(name: str, default: str = "", aliases: tuple[str, ...] = ()) -> str:
        for key in (name, *aliases):
            runtime_value = os.getenv(key)
            if runtime_value is not None and str(runtime_value).strip():
                return str(runtime_value).strip()
            file_value = values.get(key)
            if file_value is not None and file_value.strip():
                return file_value.strip()
        return default

    return env_value
