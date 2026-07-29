"""Сборка маршрутов. Логика живёт в ядре — здесь только края."""

from __future__ import annotations

from fastapi import APIRouter

from maxub.api.routes import accounts, events, messages
from maxub.api.routes.web import router as web_router

router = APIRouter()
router.include_router(events.router)
router.include_router(accounts.router)
router.include_router(messages.router)

# Веб-интерфейс подключается отдельно от API: у него своя аутентификация
# (сессия браузера вместо bearer-токена) и своя настройка включения.
__all__ = ["router", "web_router"]
