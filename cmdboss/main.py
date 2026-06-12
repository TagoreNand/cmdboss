"""Local/dev entrypoint: ``python -m cmdboss.main``.

Production runs under Gunicorn with Uvicorn workers (see ``gunicorn.conf.py``)::

    gunicorn cmdboss.app:app --config gunicorn.conf.py
"""

from __future__ import annotations

import os

import uvicorn

from .config import get_settings


def main() -> None:
    settings = get_settings()
    reload = os.getenv("CMDBOSS_RELOAD", "false").lower() in ("1", "true", "yes")
    uvicorn.run(
        "cmdboss.app:app",
        host=settings.host,
        port=settings.port,
        reload=reload,
        log_level=settings.log_level.lower(),
        log_config=None,  # we configure logging ourselves in create_app
    )


if __name__ == "__main__":
    main()
