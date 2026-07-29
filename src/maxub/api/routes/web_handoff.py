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
from maxub.api.security import authenticate
from maxub.core.access import Principal
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
    """Реестр выданных кодов входа вместе с их предъявителями.

    Код несёт того, кто его заказал: сессия, полученная в обмен, получает ровно
    его права. Иначе обмен был бы способом повысить права — ограниченный токен
    просил бы код и получал бы полноправную панель.

    Просроченное выбрасывается при обращении, а не фоновой задачей — тот же
    подход, что у реестра запросов входа в `core/challenges.py`: записей
    единицы, любое обращение и так проходит через реестр, а таск пришлось бы
    заводить, останавливать и учитывать в тестах.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, tuple[datetime, Principal]] = OrderedDict()

    def issue(self, principal: Principal) -> str:
        self._sweep()
        # Вытесняется самый старый: свежий код нужнее, чем выданный и так и не
        # использованный полминуты назад.
        while len(self._items) >= MAX_HANDOFF_CODES:
            self._items.popitem(last=False)
        code = secrets.token_urlsafe(32)
        self._items[code] = (utcnow() + HANDOFF_TTL, principal)
        return code

    def consume(self, code: str) -> Principal | None:
        """Гасит код и отдаёт предъявителя, если код годился.

        Изъятие идёт до проверки срока: код должен пропасть из реестра при любом
        обращении к нему, иначе «просроченный» и «уже использованный» стали бы
        разными состояниями с разной длиной жизни.
        """
        self._sweep()
        item = self._items.pop(code, None)
        if item is None:
            return None
        expires_at, principal = item
        return principal if expires_at > utcnow() else None

    def _sweep(self) -> None:
        now = utcnow()
        for code, (expires_at, _) in list(self._items.items()):
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


@router.post("/handoff")
async def issue_handoff(
    request: Request, principal: Principal = Depends(authenticate)
) -> dict[str, object]:
    """Выдаёт одноразовый код входа обладателю токена демона.

    Отдельных прав не требует: код открывает панель ровно с теми правами, что
    уже есть у токена, и требовать сверх этого значило бы запретить владельцу
    ограниченного токена открыть свою же панель.
    """
    return {
        "code": _codes(request).issue(principal),
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
    principal = _codes(request).consume(code) if code else None
    if principal is not None:
        issue_session(request, response, principal)
    return response
