"""Secrets vault (prototype: environment-backed).

Packs and app maps never contain secrets, only references like
`vault://BANK_PASSWORD`. Tool code resolves a reference at the moment it
needs it (e.g. typing into a login form). Values never go into prompts,
tool outputs, events or logs: the registry scrubs every resolved value from
results before anyone else sees them.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

PREFIX = "vault://"
REDACTED = "[REDACTED]"


class SecretNotFound(KeyError):
    pass


class Vault:
    def __init__(self, source: Mapping[str, str] | None = None):
        self._source = source if source is not None else os.environ
        self._seen: set[str] = set()

    @classmethod
    def from_env_file(cls, path: str = ".env") -> "Vault":
        """os.environ, plus KEY=VALUE lines from a dotenv file (environment wins)."""
        values: dict[str, str] = {}
        if os.path.exists(path):
            for line in open(path).read().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    values[k.strip()] = v.strip()
        return cls({**values, **os.environ})

    @staticmethod
    def is_ref(value: Any) -> bool:
        return isinstance(value, str) and value.startswith(PREFIX)

    def resolve(self, ref: str) -> str:
        if not self.is_ref(ref):
            raise ValueError(f"not a vault reference: {ref!r}")
        name = ref[len(PREFIX):]
        if name not in self._source:
            raise SecretNotFound(name)
        value = self._source[name]
        if value:
            self._seen.add(value)
        return value

    def scrub(self, value: Any) -> Any:
        """Recursively replace any secret value this vault has handed out."""
        if isinstance(value, str):
            for secret in self._seen:
                if secret in value:
                    value = value.replace(secret, REDACTED)
            return value
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.scrub(v) for v in value)
        return value
