"""App maps: what the employee knows about a company app it operates.

An app map is data in the company pack (packs/companies/<co>/apps/<app>.yaml):
where the app lives, how to log in (with vault references, never secrets),
how to recognise an expired session, and what is off-limits.
New app = new YAML file, no code.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field


class LoginSpec(BaseModel):
    path: str = "/login"
    fields: dict[str, str]          # accessible label -> literal or vault://REF
    submit: str                     # accessible name of the submit button


class AppMap(BaseModel):
    name: str
    title: str
    base_url: str
    read_only: bool = False
    login: LoginSpec | None = None
    logout_path: str | None = None   # landing on login after this is intentional, not an expired session
    deny_paths: list[str] = Field(default_factory=list)          # never navigate/request these
    allowed_post_paths: list[str] = Field(default_factory=list)  # for read_only apps: the only non-GET requests allowed
    notes: list[str] = Field(default_factory=list)               # hints for the Operator (playbook seeds)

    @property
    def origin(self) -> str:
        u = urlparse(self.base_url)
        return f"{u.scheme}://{u.netloc}"

    def owns(self, url: str) -> bool:
        u = urlparse(url)
        return f"{u.scheme}://{u.netloc}" == self.origin

    def is_login_page(self, url: str) -> bool:
        return self.login is not None and urlparse(url).path == self.login.path


def load_app_map(path: Path | str) -> AppMap:
    """Load one app map. `APP_URL_<NAME>` in the environment overrides base_url (e.g. in Docker)."""
    data = yaml.safe_load(Path(path).read_text())
    override = os.environ.get(f"APP_URL_{data['name'].upper()}")
    if override:
        data["base_url"] = override
    return AppMap.model_validate(data)


def load_app_maps(directory: Path | str) -> dict[str, AppMap]:
    maps = [load_app_map(p) for p in sorted(Path(directory).glob("*.yaml"))]
    return {m.name: m for m in maps}
