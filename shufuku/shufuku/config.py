from __future__ import annotations

import os
from dataclasses import dataclass


def load_dotenv(path: str = ".env") -> None:
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _s(name: str, default: str) -> str:
    return os.environ.get(name) or default


def _i(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name) or default)
    except ValueError:
        return default


@dataclass
class Config:
    anthropic_key: str = ""
    model: str = "claude-sonnet-5-5"
    host: str = "0.0.0.0"
    port: int = 8100
    access_code: str = ""
    film_normal_domestic: int = 150  # 新車塗装の膜厚の上限の目安(μm)
    film_normal_import: int = 200


def load_config() -> Config:
    load_dotenv()
    return Config(
        anthropic_key=_s("ANTHROPIC_API_KEY", ""),
        model=_s("SHUFUKU_MODEL", "claude-sonnet-5-5"),
        host=_s("SHUFUKU_HOST", "0.0.0.0"),
        port=_i("SHUFUKU_PORT", 8100),
        access_code=_s("SHUFUKU_ACCESS_CODE", ""),
        film_normal_domestic=_i("SHUFUKU_FILM_DOMESTIC", 150),
        film_normal_import=_i("SHUFUKU_FILM_IMPORT", 200),
    )
