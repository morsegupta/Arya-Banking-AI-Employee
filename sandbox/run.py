"""Run all sandbox services in one process (local dev without Docker).

    python -m sandbox.run            # ERP :8101, bank :8102, hub :8103
"""

from __future__ import annotations

import asyncio
import os

import uvicorn

SERVICES = [
    ("sandbox.erp.app:app", int(os.environ.get("ERP_PORT", 8101))),
    ("sandbox.bank_portal.app:app", int(os.environ.get("BANK_PORT", 8102))),
    ("sandbox.message_hub.app:app", int(os.environ.get("HUB_PORT", 8103))),
]


async def main() -> None:
    servers = [uvicorn.Server(uvicorn.Config(target, host="127.0.0.1", port=port, log_level="info"))
               for target, port in SERVICES]
    await asyncio.gather(*(s.serve() for s in servers))


if __name__ == "__main__":
    asyncio.run(main())
