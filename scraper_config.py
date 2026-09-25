"""Shared, JSON-backed configuration for the Tendium scraper and its management UI.

Both tendium_scraper.py and manage.py read/write the same config.json file
through this module so the web dashboard's settings form and the scraper's
actual run behavior never drift apart.
"""

import json
import os

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(PROJECT_DIR, "config.json")

DEFAULTS = {
    "headless": False,
    "target_url": "https://app.tendium.com/find/active-tenders/tender-filter/fb2112b8-c1b6-437a-99da-00c39a8371d2?tenderStatus=Awarded",
    "csv_write_mode": "append",  # "append" or "overwrite"
    "nav_timeout_ms": 45_000,
    "default_timeout_ms": 15_000,
    # Contact/contract/tag fields live on each tender's own detail page, not
    # the list view, so getting them means opening every tender found on the
    # list page one by one. That's slower and heavier on the site, hence the
    # toggle and cap below.
    "fetch_details": True,
    "max_detail_pages": 0,  # 0 = no limit
    # How many days before a contract's end date the dashboard should flag
    # it as "needs renewal soon" (a new procurement usually needs a long
    # lead time, so this defaults well above 30 days).
    "renewal_alert_days": 180,
}

# type coercion rules applied when values come in from an HTML form (strings)
_FIELD_TYPES = {
    "headless": bool,
    "target_url": str,
    "csv_write_mode": str,
    "nav_timeout_ms": int,
    "default_timeout_ms": int,
    "fetch_details": bool,
    "max_detail_pages": int,
    "renewal_alert_days": int,
}


def load_config() -> dict:
    """Return the effective config: defaults overlaid with anything in config.json."""
    cfg = dict(DEFAULTS)
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            for key in DEFAULTS:
                if key in stored:
                    cfg[key] = stored[key]
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[warn] Failed to read {CONFIG_PATH}, using defaults: {exc}")
    return cfg


def save_config(updates: dict) -> dict:
    """Merge `updates` (only known keys, coerced to the right type) into the
    stored config and write it back to disk. Returns the resulting full config.
    """
    cfg = load_config()
    for key, raw_value in updates.items():
        if key not in DEFAULTS:
            continue
        target_type = _FIELD_TYPES[key]
        if target_type is bool:
            if isinstance(raw_value, bool):
                value = raw_value
            else:
                value = str(raw_value).strip().lower() in ("1", "true", "on", "yes")
        elif target_type is int:
            try:
                value = int(raw_value)
            except (TypeError, ValueError):
                continue
        else:
            value = str(raw_value).strip()
        cfg[key] = value

    if cfg.get("csv_write_mode") not in ("append", "overwrite"):
        cfg["csv_write_mode"] = DEFAULTS["csv_write_mode"]

    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    return cfg
