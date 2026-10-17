"""Sandbox configuration from environment variables (see .env.example).

Read at call time, not import time, so tests can point services at a temp dir.
"""

from __future__ import annotations

import os
from pathlib import Path

DEFAULTS: dict[str, str] = {
    "SANDBOX_VAR_DIR": "var",
    "ERP_WEB_USER": "ar.clerk",
    "ERP_WEB_PASSWORD": "erp-sandbox-pass",
    "ERP_API_KEY": "erp-sandbox-readonly-key",
    "BANK_USER_ID": "SHARMA001",
    "BANK_PASSWORD": "bank-sandbox-pass",
    "BANK_SESSION_TTL_SECONDS": "300",
    "HUB_ARYA_TOKEN": "hub-arya-token",
    "HUB_SIM_TOKEN": "hub-sim-token",
    "SANDBOX_ADMIN_TOKEN": "sandbox-admin-token",
}


def get(key: str) -> str:
    return os.environ.get(key, DEFAULTS[key])


def var_dir() -> Path:
    path = Path(get("SANDBOX_VAR_DIR")).resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def drive_dir() -> Path:
    return var_dir() / "drive"
