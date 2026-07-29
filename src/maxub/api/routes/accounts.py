"""Аккаунты и авторизация: по телефону и по QR-коду."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from maxub.api.routes.common import (
    AccountRequest,
    AddAccountRequest,
    ChallengeRequest,
    DisableRequest,
    LoginCompleteRequest,
    get_service,
    http_error,
)
from maxub.api.security import require
from maxub.core.permissions import Scope
from maxub.core.service import ServiceError, ServiceOverloaded, UserbotService
from maxub.transport.base import TransportUnsupported

# Права проставлены на каждом маршруте отдельно: список аккаунтов и вход в
# аккаунт — разные по последствиям действия, и общая зависимость на роутере
# выдала бы читателю право входить.
router = APIRouter()

READ = Depends(require(Scope.ACCOUNTS_READ))
WRITE = Depends(require(Scope.ACCOUNTS_WRITE))


@router.get("/accounts", dependencies=[READ])
async def list_accounts(service: UserbotService = Depends(get_service)) -> list[dict[str, object]]:
    return [a.model_dump(mode="json") for a in await service.list_accounts()]


@router.post("/accounts", status_code=201, dependencies=[WRITE])
async def add_account(
    payload: AddAccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.add_account(payload.phone, payload.label)
    except ServiceError as exc:
        raise http_error(409, exc) from exc
    return account.model_dump(mode="json")


@router.post("/accounts/{account_id}/disable", dependencies=[WRITE])
async def disable_account(
    account_id: int, payload: DisableRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.disable_account(account_id, payload.reason)
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return account.model_dump(mode="json")


@router.get("/accounts/{account_id}/capabilities", dependencies=[READ])
async def account_capabilities(
    account_id: int, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    """Что умеет транспорт этого аккаунта — чтобы клиент не гадал."""
    try:
        return await service.capabilities(account_id)
    except ServiceError as exc:
        raise http_error(404, exc) from exc


# --- вход по телефону --------------------------------------------------------


@router.post("/login/start", dependencies=[WRITE])
async def login_start(
    payload: AccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, str]:
    try:
        challenge_id = await service.start_login(payload.account_id)
    except ServiceOverloaded as exc:
        raise http_error(429, exc) from exc
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return {"challenge_id": challenge_id}


@router.post("/login/complete", dependencies=[WRITE])
async def login_complete(
    payload: LoginCompleteRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        account = await service.complete_login(payload.challenge_id, payload.code)
    except ServiceError as exc:
        raise http_error(400, exc) from exc
    return account.model_dump(mode="json")


# --- вход по QR-коду ---------------------------------------------------------


@router.post("/login/qr/start", dependencies=[WRITE])
async def login_qr_start(
    payload: AccountRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    """Второй способ входа: код сканируется приложением MAX, SMS не нужен."""
    try:
        return await service.start_qr_login(payload.account_id)
    except TransportUnsupported as exc:
        raise http_error(501, exc) from exc
    except ServiceOverloaded as exc:
        raise http_error(429, exc) from exc
    except ServiceError as exc:
        raise http_error(404, exc) from exc


@router.post("/login/qr/poll", dependencies=[WRITE])
async def login_qr_poll(
    payload: ChallengeRequest, service: UserbotService = Depends(get_service)
) -> dict[str, object]:
    try:
        status_value, account = await service.poll_qr_login(payload.challenge_id)
    except ServiceError as exc:
        raise http_error(404, exc) from exc
    return {
        "status": status_value.value,
        "account": account.model_dump(mode="json") if account else None,
    }
