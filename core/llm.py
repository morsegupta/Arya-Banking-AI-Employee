"""One LLM client for every role (OpenAI-compatible chat completions; Groq in the prototype).

What it adds on top of a plain HTTP call (architecture §10, "LLM client limits"):
- Model per role from config/models.yaml, so swapping a model is a config change.
- Rate limiting from the provider's own headers: before a call, if the
  estimated tokens exceed what is left in the current window, wait for reset.
  (Free tiers can be as low as 8k tokens/min per model.)
- Retry on 429 (honouring retry-after) and on 5xx / network errors, with backoff.
- A token budget per mission: an agent stuck in a loop stops instead of burning money.
- structured(): JSON output validated against a Pydantic model, one repair retry.
- guard_score(): the prompt-injection classifier, chunked to its 512-token window.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeVar

import httpx
import yaml
from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)

# Provider error codes meaning "the model produced malformed output"; a fresh sample usually works.
MODEL_OUTPUT_ERRORS = {"tool_use_failed", "output_parse_failed", "json_validate_failed"}
RESAMPLES = 2


class LLMError(Exception):
    pass


class BudgetExceeded(LLMError):
    pass


class ToolUseFailed(LLMError):
    """The provider rejected the model's own output: a tool name not in the list, or text it could not
    parse (gpt-oss channel leaks). Not a transport problem: re-sample, then tell the model."""

    def __init__(self, message: str, failed_generation: str = ""):
        super().__init__(message)
        self.failed_generation = failed_generation


class LLMOutputError(LLMError):
    """The model did not produce valid structured output, even after a repair attempt."""


@dataclass
class RoleConfig:
    model: str
    reasoning_effort: str | None = None
    max_tokens: int | None = None
    temperature: float | None = None


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]
    raw_arguments: str


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[ToolCall]
    usage: dict[str, Any]
    reasoning: str | None = None
    finish_reason: str | None = None

    def assistant_message(self) -> dict[str, Any]:
        """The message to append to the conversation before sending tool results back."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "type": "function",
                                  "function": {"name": c.name, "arguments": c.raw_arguments}}
                                 for c in self.tool_calls]
        return msg


@dataclass
class Budget:
    """Token budget (prompt + completion) for one mission or task."""
    max_tokens: int
    used: int = 0

    def charge(self, tokens: int) -> None:
        self.used += tokens

    def check(self) -> None:
        if self.used >= self.max_tokens:
            raise BudgetExceeded(f"token budget exhausted ({self.used}/{self.max_tokens})")


_DURATION = re.compile(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m(?!s))?(?:(\d+(?:\.\d+)?)s)?(?:(\d+(?:\.\d+)?)ms)?$")


def parse_duration(text: str | None) -> float:
    """'1m26.4s' -> 86.4, '735ms' -> 0.735, '3.457s' -> 3.457."""
    if not text:
        return 0.0
    m = _DURATION.match(text.strip())
    if not m:
        try:
            return float(text)
        except ValueError:
            return 0.0
    h, mi, s, ms = (float(x) if x else 0.0 for x in m.groups())
    return h * 3600 + mi * 60 + s + ms / 1000


@dataclass
class _Window:
    remaining_tokens: int | None = None
    remaining_requests: int | None = None
    tokens_reset_at: float = 0.0
    requests_reset_at: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def estimate_tokens(messages: list[dict], tools: list[dict] | None, max_tokens: int | None) -> int:
    chars = len(json.dumps(messages)) + (len(json.dumps(tools)) if tools else 0)
    return chars // 4 + (max_tokens or 1000)


class LLMClient:
    def __init__(self, base_url: str, api_key: str, roles: dict[str, RoleConfig], *,
                 transport: httpx.AsyncBaseTransport | None = None, timeout: float = 120.0, max_attempts: int = 5,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep, clock: Callable[[], float] = time.monotonic,
                 on_wait: Callable[[str, float, str], None] | None = None):
        self.roles = roles
        self.on_wait = on_wait          # (model, seconds, reason): make throttling visible to operators
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout, transport=transport,
                                       headers={"Authorization": f"Bearer {api_key}"})
        self.max_attempts, self._sleep, self._now = max_attempts, sleep, clock
        self._windows: dict[str, _Window] = {}
        self.calls: list[dict[str, Any]] = []      # per-call usage log, for budgets and evals

    @classmethod
    def from_config(cls, path: Path | str, vault, **kw) -> "LLMClient":
        cfg = yaml.safe_load(Path(path).read_text())
        prov = cfg["provider"]
        resolve = lambda v: vault.resolve(v) if vault.is_ref(v) else v  # noqa: E731
        roles = {name: RoleConfig(**spec) for name, spec in cfg["roles"].items()}
        return cls(resolve(prov["base_url"]), resolve(prov["api_key"]), roles, **kw)

    async def close(self) -> None:
        await self._http.aclose()

    # --- core call ------------------------------------------------------------------

    async def chat(self, role: str, messages: list[dict[str, Any]], *, tools: list[dict] | None = None,
                   json_mode: bool = False, budget: Budget | None = None, tag: str = "") -> LLMResponse:
        cfg = self.roles[role]
        if budget:
            budget.check()
        body: dict[str, Any] = {"model": cfg.model, "messages": messages}
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if cfg.reasoning_effort:
            body["reasoning_effort"] = cfg.reasoning_effort
        if cfg.max_tokens:
            body["max_completion_tokens"] = cfg.max_tokens
        if cfg.temperature is not None:
            body["temperature"] = cfg.temperature

        for resample in range(RESAMPLES + 1):
            try:
                data = await self._post(cfg.model, body, estimate_tokens(messages, tools, cfg.max_tokens))
                break
            except ToolUseFailed:
                self.calls.append({"role": role, "model": cfg.model, "tag": f"{tag}:rejected", "total_tokens": 0})
                if resample == RESAMPLES:
                    raise
        choice = data["choices"][0]
        msg = choice["message"]
        calls = []
        for tc in msg.get("tool_calls") or []:
            raw = tc["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw)
            except json.JSONDecodeError:
                args = {"__invalid_json__": raw}
            calls.append(ToolCall(tc["id"], tc["function"]["name"], args if isinstance(args, dict) else {}, raw))
        usage = data.get("usage", {})
        if budget:
            budget.charge(usage.get("total_tokens", 0))
        self.calls.append({"role": role, "model": cfg.model, "tag": tag, "total_tokens": usage.get("total_tokens", 0)})
        return LLMResponse(msg.get("content"), calls, usage, msg.get("reasoning"), choice.get("finish_reason"))

    async def _post(self, model: str, body: dict, estimate: int) -> dict:
        window = self._windows.setdefault(model, _Window())
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            async with window.lock:  # one in-flight request per model keeps the header accounting honest
                await self._wait_for_capacity(model, window, estimate)
                try:
                    resp = await self._http.post("/chat/completions", json=body)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    resp, last_error = None, repr(exc)
                if resp is not None:
                    self._update_window(window, resp.headers)
            if resp is not None and resp.status_code == 200:
                return resp.json()
            if resp is not None and resp.status_code == 429:
                wait = parse_duration(resp.headers.get("retry-after")) or 2.0 * attempt
                last_error = f"429 rate limited: {resp.text[:200]}"
                self._notify(model, wait, _error_body(resp).get("message", "429 rate limited"))
                await self._sleep(wait + random.uniform(0, 0.5))
                continue
            if resp is not None and resp.status_code < 500:
                err = _error_body(resp)
                if err.get("code") in MODEL_OUTPUT_ERRORS:
                    raise ToolUseFailed(err.get("message", "tool use failed"), err.get("failed_generation", ""))
                raise LLMError(f"{model} rejected request ({resp.status_code}): {resp.text[:500]}")
            if resp is not None:
                last_error = f"{resp.status_code}: {resp.text[:200]}"
            await self._sleep(min(30.0, 1.0 * 2 ** (attempt - 1)) + random.uniform(0, 0.5))
        raise LLMError(f"{model} failed after {self.max_attempts} attempts: {last_error}")

    async def _wait_for_capacity(self, model: str, w: _Window, estimate: int) -> None:
        now = self._now()
        if w.remaining_requests is not None and w.remaining_requests <= 0 and w.requests_reset_at > now:
            self._notify(model, w.requests_reset_at - now, "request quota used up")
            await self._sleep(w.requests_reset_at - now)
            now = self._now()
        if w.remaining_tokens is not None and w.remaining_tokens < estimate and w.tokens_reset_at > now:
            self._notify(model, w.tokens_reset_at - now, f"token window: {w.remaining_tokens} left, need ~{estimate}")
            await self._sleep(w.tokens_reset_at - now)

    def _notify(self, model: str, seconds: float, reason: str) -> None:
        if self.on_wait and seconds >= 1:
            self.on_wait(model, seconds, reason)

    def _update_window(self, w: _Window, headers: httpx.Headers) -> None:
        now = self._now()
        if "x-ratelimit-remaining-tokens" in headers:
            w.remaining_tokens = int(float(headers["x-ratelimit-remaining-tokens"]))
            w.tokens_reset_at = now + parse_duration(headers.get("x-ratelimit-reset-tokens"))
        if "x-ratelimit-remaining-requests" in headers:
            w.remaining_requests = int(float(headers["x-ratelimit-remaining-requests"]))
            w.requests_reset_at = now + parse_duration(headers.get("x-ratelimit-reset-requests"))

    # --- structured output -----------------------------------------------------------

    async def structured(self, role: str, messages: list[dict[str, Any]], schema: type[T], *,
                         budget: Budget | None = None, tag: str = "") -> T:
        """JSON output validated against `schema`; on failure, one repair retry with the error."""
        instruction = ("Respond with a single JSON object only, no prose, matching this JSON schema:\n"
                       + json.dumps(schema.model_json_schema()))
        msgs = [*messages, {"role": "system", "content": instruction}]
        error = ""
        for attempt in range(2):
            resp = await self.chat(role, msgs, json_mode=True, budget=budget, tag=tag)
            text = (resp.content or "").strip()
            try:
                return schema.model_validate_json(_strip_fences(text))
            except ValidationError as exc:
                error = str(exc)
            msgs = [*msgs, {"role": "assistant", "content": text},
                    {"role": "user", "content": f"That JSON was invalid:\n{error[:1500]}\nReturn corrected JSON only."}]
        raise LLMOutputError(f"{role}: invalid structured output after repair: {error[:500]}")

    # --- injection guard -------------------------------------------------------------

    async def guard_score(self, text: str, *, chunk_chars: int = 1600, overlap: int = 200) -> float:
        """Max malicious probability over overlapping chunks (the classifier sees 512 tokens)."""
        if not text.strip():
            return 0.0
        step = chunk_chars - overlap
        chunks = [text[i:i + chunk_chars] for i in range(0, max(1, len(text) - overlap), step)] or [text]
        best = 0.0
        for chunk in chunks:
            resp = await self.chat("guard", [{"role": "user", "content": chunk}], tag="guard")
            try:
                best = max(best, float((resp.content or "0").strip()))
            except ValueError:
                raise LLMError(f"guard returned a non-numeric score: {resp.content!r}")
        return best


def _error_body(resp: httpx.Response) -> dict[str, Any]:
    try:
        body = resp.json()
    except ValueError:
        return {}
    return body.get("error", {}) if isinstance(body, dict) and isinstance(body.get("error"), dict) else {}


def _strip_fences(text: str) -> str:
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    return text.strip()
