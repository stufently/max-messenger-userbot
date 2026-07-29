"""Запуск демона."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from maxub.api.app import create_app
from maxub.config import Settings

log = logging.getLogger(__name__)


async def _watch_shutdown(app_state: object, server: uvicorn.Server) -> None:
    event: asyncio.Event = app_state.shutdown_event  # type: ignore[attr-defined]
    await event.wait()
    log.info("получена команда остановки")
    server.should_exit = True


async def serve(settings: Settings) -> None:
    settings.ensure_data_dir()
    token = settings.resolve_token()
    app = create_app(settings)
    app.state.api_token = token

    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level,
        access_log=False,
    )
    server = uvicorn.Server(config)
    watcher = asyncio.create_task(_watch_shutdown(app.state, server))
    try:
        await server.serve()
    finally:
        watcher.cancel()


def run(settings: Settings | None = None) -> None:
    asyncio.run(serve(settings or Settings()))
