"""Выпуск и отзыв токенов API.

Сырое значение токена возвращается ровно один раз — в ответе на выпуск. Демон
хранит только отпечаток и показать токен повторно не может; потерявший его
выпускает новый и отзывает старый.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status

from maxub.api.routes.common import IssueTokenRequest
from maxub.api.security import require
from maxub.core.access import AccessControl, Principal
from maxub.core.permissions import Scope

router = APIRouter(prefix="/tokens")

ADMIN = Depends(require(Scope.ADMIN))


def get_access(request: Request) -> AccessControl:
    access: AccessControl = request.app.state.access
    return access


@router.post("", status_code=201)
async def issue(
    payload: IssueTokenRequest,
    principal: Principal = ADMIN,
    access: AccessControl = Depends(get_access),
) -> dict[str, object]:
    """Выпускает токен с указанными областями доступа.

    Выдать больше, чем есть у самого выпускающего, нельзя. Иначе право
    управлять токенами оказалось бы правом на всё: обладатель одного лишь
    `admin` выписал бы себе токен с отправкой сообщений и обошёл ограничение,
    ради которого области и заведены. У корневого токена есть всё, поэтому
    владельца это правило не стесняет.
    """
    scopes = payload.parsed_scopes()
    missing = principal.missing(scopes)
    if missing:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "нельзя выдать права, которых нет у самого токена: "
                + ", ".join(scope.value for scope in missing)
            ),
        )
    raw, token = await access.issue(
        payload.label, scopes, AccessControl.expiry(payload.expires_in_days)
    )
    return {"token": raw, "item": token.model_dump(mode="json")}


@router.get("", dependencies=[ADMIN])
async def listing(
    include_revoked: bool = False, access: AccessControl = Depends(get_access)
) -> list[dict[str, object]]:
    tokens = await access.list_tokens(include_revoked)
    return [token.model_dump(mode="json") for token in tokens]


@router.delete("/{token_id}", dependencies=[ADMIN])
async def revoke(token_id: int, access: AccessControl = Depends(get_access)) -> dict[str, object]:
    """Отзывает токен. Повторный отзыв — 404: отзывать уже нечего."""
    if not await access.revoke(token_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="токен не найден или уже отозван",
        )
    return {"revoked": token_id}
