"""Serve the real application from this worktree on the M5 acceptance port.

Deliberate non-differences from production: no injected middleware, no patched
fetch, no response rewriting. The point of an acceptance server is that what the
browser sees is what the repository produces -- a probe that measures a mutated
response measures nothing. The only changes are (a) the data directory, which
points at the private acceptance copy, and (b) background jobs (antiban,
scheduled refresh, init-apply) turned off so a measurement is not racing a
sweeper.

Templates are reached through the runtime's ``templates`` symlink, which points
at this worktree's templates, so a template change here is visible without
copying anything.
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

PORT = 5025
ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "tmp/agent-team/m5-acceptance/runtime"


def main() -> int:
    if not RUNTIME.is_dir():
        raise SystemExit(f"acceptance runtime missing: {RUNTIME}")

    sys.path.insert(0, str(ROOT))
    os.chdir(RUNTIME)
    logging.disable(logging.CRITICAL)

    os.environ.update(
        ENABLE_GATEWAY="true",
        ENABLE_ANTIBAN="false",
        SCHEDULED_REFRESH="false",
        INIT_APPLY_ON_EMPTY="false",
        INIT_FORCE="false",
        FLEET_DB_PATH=str(RUNTIME / "data/chat2api.db"),
        SESSION_DB_PATH=str(RUNTIME / "data/sessions.db"),
        SMTP_HOST="", SMTP_USER="", SMTP_PASSWORD="",
    )

    import uvicorn
    from app import app

    print(f"m5 acceptance mirror ready on http://127.0.0.1:{PORT} (lifespan off)", flush=True)
    # lifespan off: startup hooks kick off refresh/health tasks that would write
    # to the account rows mid-measurement.
    uvicorn.run(app, host="127.0.0.1", port=PORT, lifespan="off",
                access_log=False, log_config=None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
