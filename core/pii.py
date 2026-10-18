"""PII tokenization: personal and account data never reaches a model in clear text.

    "Pay to A/c 50200012345678, write to ritu@x.example"
 -> "Pay to A/c {{pii:account_1}}, write to {{pii:email_1}}"

The model works with tokens; code puts the real values back into what it writes.
Two consequences:
- the model cannot leak or alter values it never saw (it cannot "correct" an account number),
- `detokenize` rejects tokens the model invented, so a draft can only carry real values.

Patterns come from the company pack (what counts as an account number differs by country).
One Tokenizer instance keeps one mapping: use one per conversation/draft.
"""

from __future__ import annotations

import re
from typing import Any

TOKEN = re.compile(r"\{\{pii:([a-z_]+_\d+)\}\}")


class UnknownToken(ValueError):
    pass


class Tokenizer:
    def __init__(self, patterns: dict[str, str]):
        self.patterns = [(kind, re.compile(p)) for kind, p in patterns.items()]
        self.by_value: dict[str, str] = {}
        self.by_token: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    def _token(self, kind: str, value: str) -> str:
        if value not in self.by_value:
            self.counts[kind] = self.counts.get(kind, 0) + 1
            tok = f"{kind}_{self.counts[kind]}"
            self.by_value[value], self.by_token[tok] = tok, value
        return "{{pii:" + self.by_value[value] + "}}"

    def tokenize(self, text: str) -> str:
        for kind, pattern in self.patterns:
            text = pattern.sub(lambda m, k=kind: self._token(k, m.group(0)), text)
        return text

    def tokenize_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.tokenize(obj)
        if isinstance(obj, dict):
            return {k: self.tokenize_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.tokenize_obj(v) for v in obj)
        return obj

    def detokenize(self, text: str) -> str:
        unknown = [t for t in TOKEN.findall(text) if t not in self.by_token]
        if unknown:
            raise UnknownToken(f"tokens that were never issued: {unknown}")
        return TOKEN.sub(lambda m: self.by_token[m.group(1)], text)

    def detokenize_obj(self, obj: Any) -> Any:
        if isinstance(obj, str):
            return self.detokenize(obj)
        if isinstance(obj, dict):
            return {k: self.detokenize_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return type(obj)(self.detokenize_obj(v) for v in obj)
        return obj

    def untokenized_values(self, text: str) -> list[str]:
        """Values in `text` that match a PII pattern but were never issued as tokens: the model
        wrote raw PII it was not given (e.g. a made-up account number)."""
        found = []
        for _, pattern in self.patterns:
            found += [m.group(0) for m in pattern.finditer(text) if m.group(0) not in self.by_value]
        return found
