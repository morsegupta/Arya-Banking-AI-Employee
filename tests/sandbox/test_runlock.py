import os

import pytest

from sandbox.common.runlock import Busy, run_lock


def test_second_holder_refused_and_stale_lock_recovered(tmp_path):
    with run_lock(tmp_path, "demo"):
        with pytest.raises(Busy, match="demo"):
            with run_lock(tmp_path, "seed"):
                pass
    assert not (tmp_path / ".run.lock").exists()
    (tmp_path / ".run.lock").write_text("999999 crashed demo")   # pid that does not exist
    with run_lock(tmp_path, "seed"):
        assert (tmp_path / ".run.lock").read_text().startswith(str(os.getpid()))
