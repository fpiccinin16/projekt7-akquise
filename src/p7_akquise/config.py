"""Lädt config/settings.yaml und config/segmente.yaml (mit Standardwerten)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = BASE_DIR / "config"

DEFAULTS: dict[str, Any] = {
    "storage": {"type": "excel", "excel_path": "data/projekt7_Leads.xlsx"},
    "pipeline": {
        "kandidaten_pro_lauf": 100,
        "import_bewertungen_pro_lauf": 60,
        "a_leads_pro_woche": 8,
        "max_seiten_pro_firma": 8,
        "b_lead_wiedervorlage_tage": 28,
    },
    "nachfass": {"werktage_bis_nachfass": 5, "max_nachfass": 2},
    "digest": {"empfaenger": "", "cc": ""},
}


def _load_yaml(name: str) -> dict[str, Any]:
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


@lru_cache(maxsize=1)
def settings() -> dict[str, Any]:
    data = _load_yaml("settings.yaml")
    merged: dict[str, Any] = {}
    for section, defaults in DEFAULTS.items():
        merged[section] = {**defaults, **(data.get(section) or {})}
    return merged


def setting(section: str, key: str, default: Any = None) -> Any:
    return settings().get(section, {}).get(key, default)


@lru_cache(maxsize=1)
def segmente() -> dict[str, Any]:
    return _load_yaml("segmente.yaml")
