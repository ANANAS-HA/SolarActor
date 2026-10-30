"""Load .env settings and configs/*.yaml into contract models."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import dotenv_values
from pydantic import BaseModel

from raqeeb.contracts import ActionSpec, ActionType, AlarmCode, PlantConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = REPO_ROOT / "configs"
DATA_DIR = REPO_ROOT / "data"
WEATHER_DIR = DATA_DIR / "weather"
SCENARIO_DIR = REPO_ROOT / "scenarios"  # the practice set (scn_*.yaml): tune only on this
TEST_SCENARIO_DIR = SCENARIO_DIR / "test"  # the held-out test set (tst_*.yaml): never tune on it
BENCH_DIR = REPO_ROOT / "bench"  # benchmark output (results.csv, summary.csv, summary.md)
ENV_FILE = REPO_ROOT / ".env"


class Settings(BaseModel):
    local_llm_base_url: str = ""
    local_llm_model: str = ""
    local_llm_api_key: str = "local"
    anthropic_api_key: str = ""
    api_llm_model: str = "anthropic/claude-sonnet-5-5"
    site_name: str = "Riyadh"
    site_lat: float = 24.71
    site_lon: float = 46.68
    weather_year: int = 2025
    raqeeb_db: str = "data/raqeeb.db"

    @property
    def local_llm_configured(self) -> bool:
        return bool(self.local_llm_base_url and self.local_llm_model)

    @property
    def db_path(self) -> Path:
        path = Path(self.raqeeb_db)
        return path if path.is_absolute() else REPO_ROOT / path

    @property
    def weather_csv(self) -> Path:
        """Processed hourly NASA POWER file written by scripts/download_weather.py."""
        return WEATHER_DIR / f"nasa_power_{self.site_lat:.2f}_{self.site_lon:.2f}_{self.weather_year}.csv"

    def __repr__(self) -> str:  # never print keys
        return f"Settings(site={self.site_name!r}, local_model={self.local_llm_model!r})"

    __str__ = __repr__


def load_settings(env_file: Path = ENV_FILE) -> Settings:
    """Process environment wins over .env; empty values fall back to defaults."""
    keys = Settings.model_fields.keys()
    values: dict[str, str] = {}
    if env_file.exists():
        values.update({k.lower(): v for k, v in dotenv_values(env_file).items() if v})
    values.update({k.lower(): v for k, v in os.environ.items() if k.lower() in keys and v})
    return Settings(**{k: v for k, v in values.items() if k in keys})


def _read_yaml(name: str) -> dict:
    return yaml.safe_load((CONFIG_DIR / name).read_text(encoding="utf-8"))


@lru_cache
def load_plant() -> PlantConfig:
    data = _read_yaml("plant.yaml")
    settings = load_settings()
    data["site"] = {
        "name": settings.site_name,
        "lat": settings.site_lat,
        "lon": settings.site_lon,
        **data.get("site", {}),
    }
    return PlantConfig.model_validate(data)


@lru_cache
def load_alarm_codes() -> dict[str, AlarmCode]:
    return {code: AlarmCode.model_validate(spec) for code, spec in _read_yaml("alarm_codes.yaml")["codes"].items()}


@lru_cache
def load_actions() -> dict[ActionType, ActionSpec]:
    return {ActionType(name): ActionSpec.model_validate(spec) for name, spec in _read_yaml("actions.yaml")["actions"].items()}
