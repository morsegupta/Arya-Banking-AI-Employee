"""Each test gets a fresh, fully seeded sandbox in its own temp dir.

Seeding (65 PDFs) runs once per session into a template dir; tests get a copy.
"""

from __future__ import annotations

import shutil

import pytest

from sandbox.common.world import World, load_world


@pytest.fixture(scope="session")
def world() -> World:
    return load_world()


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory, world):
    template = tmp_path_factory.mktemp("sandbox_template")
    mp = pytest.MonkeyPatch()
    mp.setenv("SANDBOX_VAR_DIR", str(template))
    from sandbox.seed import seed_all

    seed_all(world)
    mp.undo()
    return template


@pytest.fixture()
def sandbox_var(tmp_path, seeded_template, monkeypatch):
    var = tmp_path / "var"
    shutil.copytree(seeded_template, var)
    monkeypatch.setenv("SANDBOX_VAR_DIR", str(var))
    return var


# --- runtime fixtures ------------------------------------------------------------------

@pytest.fixture()
def runtime_db(tmp_path):
    from core.store import Database

    return Database(tmp_path / "runtime.db")


@pytest.fixture()
def events(runtime_db):
    from core.events import EventStore

    return EventStore(runtime_db)


@pytest.fixture()
def sleeps():
    """Recorded backoff delays; registry sleeps are instant in tests."""
    return []


@pytest.fixture()
def registry(runtime_db, events, sleeps):
    from tools.base import ToolRegistry
    from tools.vault import Vault

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    return ToolRegistry(runtime_db, events, vault=Vault({}), sleep=fake_sleep)


@pytest.fixture()
def ctx(tmp_path):
    from tools.base import RunContext

    def make(apps, key=None, task_id="tsk_test"):
        return RunContext(mission_id="mis_test", task_id=task_id, allowed_apps=set(apps),
                          idempotency_key=key, artifacts_dir=tmp_path / "artifacts")
    return make
