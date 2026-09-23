"""Quick Settings configuration shared by the controls window and shell tile."""

import json
import os
from pathlib import Path


CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "cmf-headphone-pro"
CONFIG_PATH = CONFIG_DIR / "config.json"

ITEMS = {
    "noise": ("Noise control", ["ANC", "Off", "Transparency"]),
    "level": ("ANC level", ["High", "Mid", "Low", "Adaptive"]),
    "lag": ("Low lag", ["Off", "On"]),
    "spatial": ("Spatial audio", ["Off", "Concert", "Cinema"]),
    "eq": ("Equaliser", ["Pop", "Rock", "Electronic", "Enhance Vocals", "Classical", "Custom"]),
    "profile": ("Personal sound profile", ["Off", "On"]),
    "find": ("Find my headphones", []),
}


def defaults():
    return {
        "enabled": True,
        "click": "noise",
        "order": list(ITEMS),
        "items": {
            key: {"enabled": key in {"noise", "level", "spatial", "lag"},
                  "options": choices.copy()}
            for key, (_title, choices) in ITEMS.items()
        },
    }


def load():
    config = defaults()
    try:
        saved = json.loads(CONFIG_PATH.read_text())
    except (OSError, ValueError):
        return config
    if not isinstance(saved, dict):
        return config

    config["enabled"] = saved.get("enabled") is not False
    order = saved.get("order")
    if isinstance(order, list):
        config["order"] = list(dict.fromkeys(key for key in order if key in ITEMS))
        config["order"] += [key for key in ITEMS if key not in config["order"]]
    saved_items = saved.get("items") if isinstance(saved.get("items"), dict) else {}
    for key, (_title, choices) in ITEMS.items():
        item = saved_items.get(key)
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("enabled"), bool):
            config["items"][key]["enabled"] = item["enabled"]
        selected = item.get("options")
        if isinstance(selected, list) and key not in {"lag", "find"}:
            selected = [choice for choice in choices if choice in selected]
            minimum = 1 if key == "profile" else 2
            if len(selected) >= minimum:
                config["items"][key]["options"] = selected
    active = [key for key in config["order"] if config["items"][key]["enabled"]]
    config["click"] = saved.get("click") if saved.get("click") in active else (active[0] if active else "")
    return config


def save(config):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n")
    temporary.replace(CONFIG_PATH)
