"""Сборка FastAPI-приложения."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from fastapi import FastAPI

from maxub.api.routes import router, web_router
from maxub.config import Settings
from maxub.core.crypto import SecretBox
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport import get_factory


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_data_dir()

    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    service = UserbotService(
        settings=settings,
        storage=storage,
        transport_factory=get_factory(settings.transport),
    )

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await service.start()
        try:
            yield
        finally:
            await service.stop()

    app = FastAPI(
        title="MAX Userbot",
        summary="Локальный API демона. Наружу не выставляется.",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.api_token = settings.resolve_token()
    app.state.shutdown_event = asyncio.Event()
    # Сессии браузера живут в памяти процесса: они короткоживущие, а переживать
    # перезапуск демона им незачем — после него токен вводится заново.
    app.state.web_sessions = {}
    app.include_router(router)
    if settings.web_ui:
        app.include_router(web_router)
    return app
