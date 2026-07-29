"""Одноразовый код входа в панель.

Зачем он нужен. Автономная сборка под Windows поднимает демон и открывает
браузер сама, но вход в панель — это ввод токена демона в форму, а 43 случайных
символа человек руками не перепишет. Раньше лаунчер клал токен в буфер обмена:
буфер переживает вставку, попадает в историю `Win+V` и уезжает в облако, если
синхронизация включена. Токен даёт полный контроль над аккаунтами и не истекает
— слишком долгая жизнь в слишком общедоступном месте.

Код заменяет одну эту вставку, а не сессию. Тот, кто уже знает токен, просит
`POST /web/handoff` и получает случайную строку, живущую пару минут и гаснущую
при первом использовании. Выдача закрыта тем же bearer-токеном, что и остальной
API: код, который может выпросить кто угодно, был бы вторым и куда более слабым
способом входа. Bearer-маршруты при этом не меняются — ни код, ни cookie они
по-прежнему не принимают.

Почему обмен заканчивается перенаправлением, а не отрисовкой панели на месте.
Адрес с кодом обязан исчезнуть из адресной строки: иначе его копируют вместе со
ссылкой, кладут в закладки и пересылают. Ответ 303 на `/web` убирает адрес до
того, как страница вообще загрузится, и не оставляет отдельной записи в истории
переходов — назад пользователь возвращается мимо адреса с кодом. Вариант с
`history.replaceState` уже на странице слабее: он требует, чтобы код доехал до
JavaScript, и оставляет окно, в котором адрес с кодом виден и уже записан.
Полностью из истории браузера адрес всё равно не исчезает (браузеры хранят
цепочки перенаправлений) — именно поэтому код одноразовый и короткоживущий:
записанное в историю к моменту чтения уже ничего не открывает.

Неверный, погашенный и истёкший коды ведут туда же — на `/web`, но без cookie.
Пользователь увидит форму ввода токена, то есть штатный запасной вход, а
одинаковый ответ на все три случая не подсказывает тому, кто перебирает коды,
насколько он близок.
"""

from __future__ import annotations

import secrets
from collections import OrderedDict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import RedirectResponse

from maxub.api.routes.web_session import SECURITY_HEADERS, issue_session
from maxub.api.security import require_token
from maxub.core.models import utcnow

#: Куда ведёт обмен и откуда он начинается. Пути знает лаунчер
#: (`winlauncher.py`), поэтому они собраны рядом и меняются вместе.
ENTER_PATH = "/web/enter"
PAGE_PATH = "/web"
CODE_PARAM = "code"

#: Срока хватает на запуск браузера с холодного старта, но не на то, чтобы
#: подобранный из истории адрес пригодился кому-то позже.
HANDOFF_TTL = timedelta(minutes=2)
#: Потолок живых кодов: лаунчер просит один код на запуск, а обладателю токена
#: незачем копить их пачками. Ограничение держит расход памяти конечным, даже
#: если выдачу дёргают в цикле.
MAX_HANDOFF_CODES = 8


class HandoffCodes:
    """Реестр выданных кодов входа.

    Просроченное выбрасывается при обращении, а не фоновой задачей — тот же
    подход, что у реестра запросов входа в `core/challenges.py`: записей
    единицы, любое обращение и так проходит через реестр, а таск пришлось бы
    заводить, останавливать и учитывать в тестах.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, datetime] = OrderedDict()

    def issue(self) -> str:
        self._sweep()
        # Вытесняется самый старый: свежий код нужнее, чем выданный и так и не
        # использованный полминуты назад.
        while len(self._items) >= MAX_HANDOFF_CODES:
            self._items.popitem(last=False)
        code = secrets.token_urlsafe(32)
        self._items[code] = utcnow() + HANDOFF_TTL
        return code

    def consume(self, code: str) -> bool:
        """Гасит код и говорит, годился ли он.

        Изъятие идёт до проверки срока: код должен пропасть из реестра при любом
        обращении к нему, иначе «просроченный» и «уже использованный» стали бы
        разными состояниями с разной длиной жизни.
        """
        self._sweep()
        expires_at = self._items.pop(code, None)
        return expires_at is not None and expires_at > utcnow()

    def _sweep(self) -> None:
        now = utcnow()
        for code, expires_at in list(self._items.items()):
            if expires_at <= now:
                del self._items[code]


router = APIRouter(include_in_schema=False)


def _codes(request: Request) -> HandoffCodes:
    """Реестр кодов этого приложения, создаваемый при первом обращении.

    Реестр нужен только панели, а панель выключается настройкой (`web_ui`) —
    заводить его в сборке приложения значило бы держать состояние веба там, где
    маршрутов `/web/*` может не быть вовсе. Гонки здесь нет: обработчики
    асинхронные и между проверкой и записью не отдают управление циклу событий.
    """
    codes: HandoffCodes | None = getattr(request.app.state, "web_handoffs", None)
    if codes is None:
        codes = HandoffCodes()
        request.app.state.web_handoffs = codes
    return codes


@router.post("/handoff", dependencies=[Depends(require_token)])
async def issue_handoff(request: Request) -> dict[str, object]:
    """Выдаёт одноразовый код входа обладателю токена демона."""
    return {
        "code": _codes(request).issue(),
        "enter_path": ENTER_PATH,
        "expires_in": int(HANDOFF_TTL.total_seconds()),
    }


@router.get("/enter")
async def enter(
    request: Request, code: str = Query(default="", max_length=512)
) -> RedirectResponse:
    """Меняет одноразовый код на сессию браузера и уводит адрес с кодом.

    303, а не 302: браузер обязан пойти дальше именно `GET`-ом, а не повторить
    исходный метод. Заголовки безопасности проставляются здесь вручную — до
    готового `Response` то, что выставила зависимость `secure_headers`, не
    доходит; для `no-store` это принципиально, кэшировать перенаправление с
    `Set-Cookie` нельзя.
    """
    response = RedirectResponse(
        url=PAGE_PATH, status_code=status.HTTP_303_SEE_OTHER, headers=dict(SECURITY_HEADERS)
    )
    if code and _codes(request).consume(code):
        issue_session(request, response)
    return response
