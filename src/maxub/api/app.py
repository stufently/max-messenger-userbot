"""Сборка FastAPI-приложения."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Sequence

from fastapi import FastAPI

from maxub.api.routes import router, web_router
from maxub.config import Settings
from maxub.core.access import AccessControl
from maxub.core.crypto import SecretBox
from maxub.core.handlers import EventHandler
from maxub.core.service import UserbotService
from maxub.core.storage import Storage
from maxub.transport import get_factory


def create_app(settings: Settings | None = None, handlers: Sequence[EventHandler] = ()) -> FastAPI:
    settings = settings or Settings()
    settings.ensure_data_dir()

    storage = Storage(settings.db_path, SecretBox(settings.resolve_secret_key()))
    service = UserbotService(
        settings=settings,
        storage=storage,
        transport_factory=get_factory(settings.transport),
        # Обработчики передаются сборкой приложения, а не собираются демоном из
        # каталога: загрузка плагинов извне отложена за пределы v1, а контракт
        # уже есть, и подключить обработчик в своей сборке можно сегодня.
        handlers=handlers,
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
    # Проверка токенов держится на том же хранилище, что и всё остальное, но
    # живёт отдельно от сервиса: «кому можно» — вопрос не про аккаунты и не про
    # очередь, и мешать его с ними значило бы протащить права во все операции
    # ядра сразу.
    app.state.access = AccessControl(storage, app.state.api_token)
    app.state.shutdown_event = asyncio.Event()
    # Сессии браузера живут в памяти процесса: они короткоживущие, а переживать
    # перезапуск демона им незачем — после него токен вводится заново.
    app.state.web_sessions = {}
    app.include_router(router)
    if settings.web_ui:
        app.include_router(web_router)
    return app
