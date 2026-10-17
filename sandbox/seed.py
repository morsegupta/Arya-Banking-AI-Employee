"""Reset the whole sandbox company from a world file.

    python -m sandbox.seed [--world evals/worlds/sharma_default.yaml]

Wipes and rebuilds var/: clock, ERP, bank portal, message hub, shared drive.
Running it twice gives byte-identical ERP data and documents (evals depend on this).
"""

from __future__ import annotations

import argparse

from sandbox.bank_portal import db as bank_db
from sandbox.common import clock, config
from sandbox.common.runlock import Busy, run_lock
from sandbox.common.world import DEFAULT_WORLD, World, load_world
from sandbox.drive.generate import generate
from sandbox.erp import db as erp_db
from sandbox.message_hub import db as hub_db


def seed_all(world: World) -> dict:
    clock.set_now(world.start)
    erp_db.seed(world)
    bank_db.seed(world)
    hub_db.seed(world)
    drive_counts = generate(world, config.drive_dir())
    return {
        "world": world.world_id,
        "clock": world.start.isoformat(),
        "customers": len(world.customers),
        "invoices": len(world.invoices),
        "bank_transactions": len(world.bank_transactions),
        "drive": drive_counts,
        "var_dir": str(config.var_dir()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--world", default=str(DEFAULT_WORLD))
    args = parser.parse_args()
    try:
        with run_lock(config.var_dir(), "seed"):
            summary = seed_all(load_world(args.world))
    except Busy as exc:
        raise SystemExit(f"refusing to re-seed: {exc}")
    for key, value in summary.items():
        print(f"{key:>18}: {value}")


if __name__ == "__main__":
    main()
