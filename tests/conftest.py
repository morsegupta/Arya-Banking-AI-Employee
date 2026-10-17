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
