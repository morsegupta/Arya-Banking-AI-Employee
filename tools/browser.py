"""Playwright browser driver + per-app browser tools.

How the Operator sees a page: `observe()` returns a compact list of
interactive elements, each tagged with a short ref (e1, e2, ...), plus the
page's main text and any alerts. The model acts by ref: click e7, fill e3.

Safety and reliability built into the driver (not the prompt):
- One isolated browser context per task (no cookie bleed between tasks).
- Logins are done by the driver from vault references; the model never sees
  or types credentials, and password field values are masked in observations.
- Network route guard: only hosts from app maps are reachable; deny-listed
  paths are blocked; a read-only app (the bank) may only issue GET requests
  plus its login/logout POSTs. A rogue "transfer" click is blocked at the
  network layer, whatever the model intended.
- Session expiry is detected and repaired (L1 local recovery): re-login,
  restore the page, tell the model to repeat its last action.
- Every click waits for any navigation it triggers before observing again.
"""

from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from typing import Any, ClassVar
from decimal import Decimal, InvalidOperation
from urllib.parse import parse_qs, urljoin, urlparse

from playwright.async_api import Browser, BrowserContext, Page, Playwright, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from pydantic import BaseModel, Field

from core.models import Evidence, ToolResult
from tools.app_map import AppMap
from tools.base import PermanentToolError, RunContext, Tool, ToolDenied, ToolRegistry, TransientToolError
from tools.vault import Vault

NAV_SETTLE_MS = 3000

TABLES_JS = r"""
() => [...document.querySelectorAll('table')].map(t => ({
  label: t.getAttribute('aria-label') || t.querySelector('caption')?.innerText.trim() || '',
  headers: [...t.querySelectorAll('thead th')].map(th => th.innerText.trim()),
  rows: [...t.querySelectorAll('tbody tr')].map(tr => [...tr.querySelectorAll('td')].map(td => td.innerText.trim()))
}))
"""

SNAPSHOT_JS = r"""
() => {
  document.querySelectorAll('[data-op-ref]').forEach(e => e.removeAttribute('data-op-ref'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link]';
  const labelOf = el => {
    const aria = el.getAttribute('aria-label');
    if (aria) return aria;
    if (el.id) {
      const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`);
      if (l) return l.innerText.trim();
    }
    const wrap = el.closest('label');
    if (wrap) return wrap.innerText.trim();
    return (el.innerText || el.value || el.placeholder || el.name || '').trim();
  };
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(sel)) {
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden') continue;
    const ref = 'e' + (++n);
    el.setAttribute('data-op-ref', ref);
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || '').toLowerCase();
    let role = el.getAttribute('role');
    if (!role) {
      if (tag === 'a') role = 'link';
      else if (tag === 'button') role = 'button';
      else if (tag === 'select') role = 'combobox';
      else if (tag === 'textarea') role = 'textbox';
      else if (tag === 'input') role = ['submit', 'button'].includes(type) ? 'button'
        : type === 'checkbox' ? 'checkbox' : type === 'radio' ? 'radio' : 'textbox';
      else role = tag;
    }
    const item = {ref, role, name: labelOf(el).replace(/\s+/g, ' ').slice(0, 120)};
    if (type && !['submit', 'button'].includes(type)) item.type = type;
    if (tag === 'a') item.href = el.getAttribute('href');
    if (type === 'password') item.value = el.value ? '********' : '';
    else if (tag === 'input' || tag === 'textarea') item.value = el.value;
    if (tag === 'select') { item.value = el.value; item.options = [...el.options].map(o => o.value); }
    if (el.form && (tag === 'button' || type === 'submit')) {
      item.submits = (el.form.getAttribute('method') || 'get').toUpperCase() + ' ' +
                     (el.form.getAttribute('action') || location.pathname);
    }
    out.push(item);
  }
  const main = document.querySelector('main') || document.body;
  const alerts = [...document.querySelectorAll('[role=alert],[role=status]')].map(a => a.innerText.trim());
  return {elements: out, text: main.innerText.slice(0, 6000), alerts};
}
"""


class Observation(BaseModel):
    app: str
    url: str
    title: str
    elements: list[dict[str, Any]]
    text: str
    alerts: list[str] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)   # driver messages to the model (e.g. re-login happened)


def _same_value(got: str | None, want: Any) -> bool:
    """Form values compared as numbers when both are numeric ('54000' == '54000.00'), else exactly."""
    if got is None:
        return False
    try:
        return Decimal(got.replace(",", "").strip()) == Decimal(str(want).replace(",", "").strip())
    except InvalidOperation:
        return got.strip() == str(want).strip()


class BrowserDriver:
    """One task's browser: an isolated context and a single page."""

    def __init__(self, context: BrowserContext, apps: dict[str, AppMap], vault: Vault, artifacts: Path):
        self.context, self.apps, self.vault, self.artifacts = context, apps, vault, artifacts
        self.page: Page | None = None
        self.blocked: list[str] = []
        self.relogins = 0
        self.write_scope: dict[str, list[dict[str, Any]]] = {}   # set from the task's RunContext
        self.writes_used: list[str] = []

    async def start(self) -> None:
        await self.context.route("**/*", self._guard)
        self.page = await self.context.new_page()

    async def close(self) -> None:
        await self.context.close()

    # --- network guard ----------------------------------------------------------------

    def app_for(self, url: str) -> AppMap | None:
        return next((a for a in self.apps.values() if a.owns(url)), None)

    def violation(self, method: str, url: str, body: str | None = None, *, consume: bool = True) -> str | None:
        if url.startswith(("about:", "data:", "blob:")):
            return None
        app = self.app_for(url)
        if app is None:
            return f"host not in any app map: {urlparse(url).netloc}"
        path = urlparse(url).path
        if any(path.startswith(p) for p in app.deny_paths):
            return f"{path} is deny-listed for {app.name}"
        if app.read_only and method != "GET" and path not in app.allowed_post_paths:
            return f"{app.name} is read-only: {method} {path} not allowed"
        session_paths = {app.login.path if app.login else None, app.logout_path, *app.allowed_post_paths}
        if app.scoped_writes and method != "GET" and path not in session_paths:
            return self._scope_violation(app.name, method, path, body or "", consume=consume)
        return None

    def _scope_violation(self, app: str, method: str, path: str, body: str, *, consume: bool) -> str | None:
        form = {k: v[-1] for k, v in parse_qs(body, keep_blank_values=True).items()}
        for i, grant in enumerate(self.write_scope.get(app, [])):
            key = f"{app}:{i}"
            if key in self.writes_used or grant.get("method", "POST") != method or grant["path"] != path:
                continue
            wrong = [f for f, want in grant.get("form", {}).items() if not _same_value(form.get(f), want)]
            if wrong:
                return f"{method} {path}: fields {wrong} differ from what this task may submit"
            if consume:
                self.writes_used.append(key)        # each granted write can happen once
            return None
        return f"{method} {path} is not a write this task was granted"

    async def _guard(self, route: Route) -> None:
        req = route.request
        reason = self.violation(req.method, req.url, req.post_data)
        if reason:
            self.blocked.append(f"{req.method} {urlparse(req.url).path}: {reason}")
            await route.abort("blockedbyclient")
        else:
            await route.continue_()

    # --- navigation -------------------------------------------------------------------

    def _app(self, name: str) -> AppMap:
        if name not in self.apps:
            raise ToolDenied(f"no app map for {name}")
        return self.apps[name]

    async def _goto(self, url: str) -> None:
        reason = self.violation("GET", url)
        if reason:
            raise ToolDenied(reason)
        try:
            resp = await self.page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise TransientToolError(f"timeout loading {url}") from exc
        except PlaywrightError as exc:
            raise TransientToolError(f"could not load {url}: {exc.message.splitlines()[0]}") from exc
        if resp is not None and (resp.status >= 500 or resp.status == 429):
            raise TransientToolError(f"{urlparse(url).path} answered HTTP {resp.status}; try again shortly")

    async def login(self, app: AppMap) -> None:
        if app.login is None:
            raise PermanentToolError(f"{app.name} has no login spec")
        await self._goto(urljoin(app.base_url, app.login.path))
        for label, value in app.login.fields.items():
            secret = self.vault.resolve(value) if Vault.is_ref(value) else value
            await self.page.get_by_label(label, exact=True).fill(secret)
        await self.page.get_by_role("button", name=app.login.submit, exact=True).click()
        try:
            await self.page.wait_for_url(lambda u: not app.is_login_page(u), timeout=NAV_SETTLE_MS * 2)
        except PlaywrightTimeout:
            alerts = await self.page.locator("[role=alert]").all_inner_texts()
            raise PermanentToolError(f"login to {app.name} failed: {'; '.join(alerts) or 'still on login page'}")
        await self.page.wait_for_load_state("domcontentloaded")

    async def open(self, app_name: str, path: str = "/") -> Observation:
        """Go to a page of an app, logging in first if the app sends us to its login page."""
        app = self._app(app_name)
        target = urljoin(app.base_url, path)
        await self._goto(target)
        notices = []
        if app.is_login_page(self.page.url) and not app.is_login_page(target):
            await self.login(app)
            self.relogins += 1
            notices.append(f"logged in to {app.name}")
            await self._goto(target)
            if app.is_login_page(self.page.url):
                raise PermanentToolError(f"still on login page after logging in to {app.name}")
        return await self.observe(notices)

    async def observe(self, notices: list[str] | None = None) -> Observation:
        if self.page is None or self.page.url == "about:blank":
            raise PermanentToolError("no page open; use open first")
        data = await self.page.evaluate(SNAPSHOT_JS)
        app = self.app_for(self.page.url)
        return Observation(app=app.name if app else "?", url=self.page.url, title=await self.page.title(),
                           notices=notices or [], **data)

    # --- actions ------------------------------------------------------------------------

    def _locate(self, ref: str):
        return self.page.locator(f'[data-op-ref="{ref}"]')

    async def _ensure_ref(self, ref: str) -> dict[str, Any]:
        loc = self._locate(ref)
        if await loc.count() != 1:
            raise PermanentToolError(f"element {ref} not found; take a new snapshot (refs reset on every page)")
        return await loc.evaluate("""el => ({tag: el.tagName.toLowerCase(), href: el.getAttribute('href'),
            submits: el.form ? [(el.form.getAttribute('method')||'get').toUpperCase(),
                                el.form.getAttribute('action')||location.pathname] : null,
            body: el.form ? new URLSearchParams(new FormData(el.form)).toString() : null,
            type: (el.getAttribute('type')||'').toLowerCase()})""")

    async def click(self, ref: str) -> Observation:
        info = await self._ensure_ref(ref)
        before = self.page.url
        app = self.app_for(before)
        # Pre-check the obvious destination so we fail with a clear reason, not a network error.
        if info["href"]:
            reason = self.violation("GET", urljoin(before, info["href"]))
            if reason:
                raise ToolDenied(reason)
        if info["submits"] and (info["tag"] == "button" or info["type"] == "submit"):
            method, action = info["submits"]
            reason = self.violation(method, urljoin(before, action), info["body"], consume=False)
            if reason:
                raise ToolDenied(reason)
        target = info["submits"][1] if info["submits"] else info["href"]
        intentional_logout = bool(app and app.logout_path and target
                                  and urlparse(urljoin(before, target)).path == app.logout_path)
        blocked_before = len(self.blocked)
        try:
            await self._locate(ref).click(timeout=NAV_SETTLE_MS * 2)
        except PlaywrightTimeout as exc:
            raise TransientToolError(f"click {ref} timed out") from exc
        await self._settle(before)
        if len(self.blocked) > blocked_before:
            raise ToolDenied(f"blocked: {self.blocked[-1]}")
        if (app and not intentional_logout and app.is_login_page(self.page.url)
                and not app.is_login_page(before)):
            # Session expired mid-task: log back in, restore where we were, ask the model to repeat.
            await self.login(app)
            self.relogins += 1
            await self._goto(before)
            return await self.observe([f"session on {app.name} had expired; logged in again and returned to "
                                       f"{urlparse(before).path}. Your last action did not happen; repeat it."])
        return await self.observe()

    async def _settle(self, before: str) -> None:
        """Wait for a navigation the action may have started (morning lesson: never race a submit)."""
        try:
            await self.page.wait_for_url(lambda u: u != before, timeout=800)
        except PlaywrightTimeout:
            pass  # no navigation: in-page action
        try:
            await self.page.wait_for_load_state("domcontentloaded", timeout=NAV_SETTLE_MS)
        except PlaywrightTimeout as exc:
            raise TransientToolError("page did not finish loading") from exc

    async def fill(self, ref: str, value: str) -> Observation:
        info = await self._ensure_ref(ref)
        if Vault.is_ref(value):
            raise ToolDenied("vault references cannot be typed by the model; logins are handled by the driver")
        if info["type"] == "password":
            raise ToolDenied("the model may not fill password fields")
        await self._locate(ref).fill(value)
        return await self.observe()

    async def select(self, ref: str, value: str) -> Observation:
        await self._ensure_ref(ref)
        try:
            await self._locate(ref).select_option(value)
        except PlaywrightError as exc:
            raise PermanentToolError(f"cannot select {value!r} in {ref}: {exc.message.splitlines()[0]}") from exc
        return await self.observe()

    async def read_tables(self) -> list[dict[str, Any]]:
        """Every table on the page as {label, headers, rows}: reading what a person would read."""
        if self.page is None or self.page.url == "about:blank":
            raise PermanentToolError("no page open; use open first")
        return await self.page.evaluate(TABLES_JS)

    async def download(self, ref: str, dest_dir: Path) -> Evidence:
        info = await self._ensure_ref(ref)
        if info["href"]:
            reason = self.violation("GET", urljoin(self.page.url, info["href"]))
            if reason:
                raise ToolDenied(reason)
        try:
            async with self.page.expect_download(timeout=NAV_SETTLE_MS * 3) as pending:
                await self._locate(ref).click()
            dl = await pending.value
        except PlaywrightTimeout as exc:
            if self.app_for(self.page.url) and self.app_for(self.page.url).is_login_page(self.page.url):
                raise TransientToolError("session expired before the download; open the page again") from exc
            raise TransientToolError(f"no download started from {ref}") from exc
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / dl.suggested_filename
        await dl.save_as(path)
        data = path.read_bytes()
        return Evidence(kind="file", ref=str(path), summary=f"downloaded {path.name} ({len(data)} bytes)",
                        data={"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "source": dl.url})

    async def screenshot(self, dest_dir: Path, label: str) -> Evidence:
        dest_dir.mkdir(parents=True, exist_ok=True)
        path = dest_dir / f"{label}.png"
        await self.page.screenshot(path=path, full_page=True)
        return Evidence(kind="screenshot", ref=str(path), summary=f"screenshot of {self.page.url}",
                        data={"url": self.page.url})


class BrowserSessions:
    """Owns Playwright; hands each task its own isolated driver.

    Playwright belongs to the event loop that started it. If the sessions are used from a new loop
    (a later asyncio.run), the old browser is abandoned and a fresh one started, instead of awaiting
    objects of a dead loop (which hangs)."""

    def __init__(self, apps: dict[str, AppMap], vault: Vault, *, headless: bool = True):
        self.apps, self.vault, self.headless = apps, vault, headless
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._drivers: dict[str, BrowserDriver] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lock: asyncio.Lock | None = None

    def _bind(self) -> bool:
        """True if our Playwright lives on the running loop. Otherwise forget it and start over here."""
        loop = asyncio.get_running_loop()
        if self._loop is loop:
            return True
        self._drivers, self._browser, self._pw = {}, None, None     # the old loop's objects cannot be awaited
        self._loop, self._lock = loop, asyncio.Lock()
        return False

    async def driver(self, ctx: RunContext) -> BrowserDriver:
        key = ctx.task_id or "adhoc"
        self._bind()
        async with self._lock:
            if key not in self._drivers:
                if self._browser is None:
                    self._pw = await async_playwright().start()
                    self._browser = await self._pw.chromium.launch(headless=self.headless)
                context = await self._browser.new_context(accept_downloads=True)
                driver = BrowserDriver(context, self.apps, self.vault, ctx.task_dir())
                await driver.start()
                self._drivers[key] = driver
            self._drivers[key].write_scope = ctx.write_scope
            return self._drivers[key]

    async def end_task(self, task_id: str) -> None:
        if not self._bind():
            return
        async with self._lock:
            driver = self._drivers.pop(task_id, None)
        if driver:
            await driver.close()

    async def shutdown(self) -> None:
        if not self._bind():
            return
        for task_id in list(self._drivers):
            await self.end_task(task_id)
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None


# --- tools (one set per app, so capability scope stays per app) ---------------------------

class OpenArgs(BaseModel):
    path: str = Field("/", description="Path within the app, e.g. /statement?from_date=2026-10-01")


class RefArgs(BaseModel):
    ref: str = Field(description="Element ref from the latest snapshot, e.g. e7")


class FillArgs(RefArgs):
    value: str


class ScreenshotArgs(BaseModel):
    label: str = Field("page", pattern=r"^[A-Za-z0-9_-]{1,40}$")


class NoArgs(BaseModel):
    pass


class _BrowserTool(Tool):
    kind: ClassVar[str] = "dom"
    action: ClassVar[str]

    def __init__(self, app: AppMap, sessions: BrowserSessions):
        self.app = app.name
        self.app_map = app
        self.sessions = sessions
        self.name = f"{app.name}.{self.action}"
        writable_action = self.action in {"click", "fill", "select"}
        self.risk = "write" if writable_action and not app.read_only else "read"
        # On a writable app a click may submit a form: the registry then demands an idempotency key.
        self.idempotent = not (self.action == "click" and not app.read_only)

    @staticmethod
    def ok(obs: Observation, evidence: list[Evidence] | None = None) -> ToolResult:
        return ToolResult(ok=True, output=obs.model_dump(), evidence=evidence or [], untrusted=True)


class OpenTool(_BrowserTool):
    action = "open"
    description = "Open a page of this app (logs in automatically if needed) and return a snapshot."
    input_schema = OpenArgs

    async def run(self, args: OpenArgs, ctx: RunContext) -> ToolResult:
        return self.ok(await (await self.sessions.driver(ctx)).open(self.app, args.path))


class SnapshotTool(_BrowserTool):
    action = "snapshot"
    description = "Snapshot the current page: interactive elements with refs, main text, alerts."
    input_schema = NoArgs

    async def run(self, args: NoArgs, ctx: RunContext) -> ToolResult:
        return self.ok(await (await self.sessions.driver(ctx)).observe())


class ClickTool(_BrowserTool):
    action = "click"
    description = "Click an element by ref; waits for any navigation, returns the new snapshot."
    input_schema = RefArgs

    async def run(self, args: RefArgs, ctx: RunContext) -> ToolResult:
        return self.ok(await (await self.sessions.driver(ctx)).click(args.ref))


class FillTool(_BrowserTool):
    action = "fill"
    description = "Type a value into a text field by ref. Cannot fill password fields."
    input_schema = FillArgs

    async def run(self, args: FillArgs, ctx: RunContext) -> ToolResult:
        return self.ok(await (await self.sessions.driver(ctx)).fill(args.ref, args.value))


class SelectTool(_BrowserTool):
    action = "select"
    description = "Choose an option of a dropdown by ref and option value."
    input_schema = FillArgs

    async def run(self, args: FillArgs, ctx: RunContext) -> ToolResult:
        return self.ok(await (await self.sessions.driver(ctx)).select(args.ref, args.value))


class DownloadTool(_BrowserTool):
    action = "download"
    description = "Click a download link by ref and save the file as task evidence. Returns its path."
    input_schema = RefArgs

    async def run(self, args: RefArgs, ctx: RunContext) -> ToolResult:
        driver = await self.sessions.driver(ctx)
        ev = await driver.download(args.ref, ctx.task_dir() / "downloads")
        return ToolResult(ok=True, output={"path": ev.ref, **ev.data}, evidence=[ev])


class ReadTableTool(_BrowserTool):
    action = "read_table"
    description = "Read the tables on the current page as headers + rows of text (no refs needed)."
    input_schema = NoArgs

    async def run(self, args: NoArgs, ctx: RunContext) -> ToolResult:
        driver = await self.sessions.driver(ctx)
        return ToolResult(ok=True, output={"url": driver.page.url, "tables": await driver.read_tables()},
                          untrusted=True)


class ScreenshotTool(_BrowserTool):
    action = "screenshot"
    description = "Save a full-page screenshot as evidence."
    input_schema = ScreenshotArgs

    async def run(self, args: ScreenshotArgs, ctx: RunContext) -> ToolResult:
        driver = await self.sessions.driver(ctx)
        ev = await driver.screenshot(ctx.task_dir() / "screenshots", args.label)
        return ToolResult(ok=True, output={"path": ev.ref}, evidence=[ev])


BROWSER_TOOLS = (OpenTool, SnapshotTool, ClickTool, FillTool, SelectTool, DownloadTool, ReadTableTool, ScreenshotTool)


def register_browser_tools(registry: ToolRegistry, sessions: BrowserSessions) -> None:
    for app in sessions.apps.values():
        for cls in BROWSER_TOOLS:
            registry.register(cls(app, sessions))
