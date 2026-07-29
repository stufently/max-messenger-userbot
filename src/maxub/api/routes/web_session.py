"""Вход браузера в панель: обмен токена на сессию и защита от CSRF.

Почему у веба своя аутентификация, а не bearer-токен основного API.
Заголовок ``Authorization`` — правильный способ для CLI и скриптов: чужая
страница в браузере его не подделает. Для самого браузера тот же способ означал
бы хранение токена в JavaScript (и, как правило, в ``localStorage``) — любая
XSS на странице отдала бы полный контроль над аккаунтами навсегда. Поэтому веб
получает собственный вход, а маршруты API остаются нетронутыми и по-прежнему
требуют токен: веб-интерфейс не может ослабить то, чего не касается.

Как страница получает право обращаться к API:

1. Пользователь один раз вводит токен демона (тот же, что у CLI) в форму.
2. Демон сверяет его постоянным по времени сравнением и выдаёт случайный
   идентификатор сессии в cookie ``HttpOnly`` + ``SameSite=Strict`` с
   ``Path=/web``. ``HttpOnly`` — чтобы скрипт на странице не мог прочитать и
   утащить признак доступа; ``Path`` — чтобы cookie не отправлялась на
   bearer-маршруты и не превращалась там во второй способ входа. ``Secure``
   выставляется только для https: демон штатно слушает ``http://127.0.0.1``, и
   безусловный ``Secure`` просто сломал бы локальную работу.
3. Сам токен в браузере не сохраняется: он уходит один раз в теле запроса.
   Сессия живёт в памяти процесса и умирает вместе с ним.

У шага 1 есть замена для автономной сборки под Windows, где браузер открывает
сам лаунчер: одноразовый код входа, см.
[web_handoff][maxub.api.routes.web_handoff]. Дальше всё то же самое — та же
cookie, та же метка CSRF; вход по токену в форму никуда не девается, потому что
в Docker лаунчера нет вовсе.

CSRF. Одной cookie мало: демон слушает localhost, и любая открытая в том же
браузере страница может отправить на него запрос. Защита двойная — cookie с
``SameSite=Strict`` вообще не прикладывается к межсайтовым запросам, а каждый
изменяющий запрос обязан нести заголовок ``X-CSRF-Token`` со значением сессии.
Заголовок нештатный, поэтому кросс-доменный запрос уходит в preflight, а CORS
демон не разрешает; прочитать значение через ``GET /web/session`` чужая
страница тоже не может — ответ ей недоступен без CORS-заголовков.

Перепривязка DNS. Имя ``evil.com`` можно отрезолвить в 127.0.0.1, и тогда
страница злоумышленника окажется с панелью в одном origin. Сессию она этим не
получит — cookie привязана к хосту ``127.0.0.1``, а не к порту, и на
``evil.com:8765`` не отправится, — но обращаться к демону сможет. Поэтому
маршруты ``/web/*`` дополнительно требуют петлевой ``Host``.

Что этим не закрывается: локальный процесс, который и так может прочитать файл
токена. Против него защиты нет и быть не может — это тот же уровень доступа,
что у демона.
"""

from __future__ import annotations

import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from maxub.core.access import AccessControl, Principal
from maxub.core.models import utcnow
from maxub.core.permissions import Scope, format_scopes

SESSION_COOKIE = "maxub_web"
SESSION_TTL = timedelta(hours=12)
# Больше сессий одному локальному демону не нужно, а ограничение не даёт
# бесконечно накапливать записи, если кто-то дёргает форму входа.
MAX_SESSIONS = 32

# Имена, по которым панель открывается штатно. Всё остальное — либо чужое имя,
# отрезолвленное в 127.0.0.1, либо прокси, которого у демона по замыслу нет.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
# Значения `host`, при которых сужать нечего: владелец уже открыл демон наружу.
WILDCARD_HOSTS = frozenset({"", "0.0.0.0", "::"})

# Панель не кэшируется и не встраивается в чужие страницы, а CSP перечисляет
# ровно то, что ей нужно: свои файлы и data-URI для картинки QR. Сторонних
# источников в списке нет — страница обязана работать без интернета.
SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; form-action 'none'; "
        "base-uri 'none'; frame-ancestors 'none'"
    ),
}


@dataclass(slots=True)
class WebSession:
    """Сессия браузера вместе с тем, чьи права она несёт.

    Права хранятся не значением, а ссылкой на предъявителя: слепок прав жил бы
    до конца срока сессии, и отозванный час назад токен продолжал бы работать
    через открытую вкладку. Перед каждым защищённым запросом предъявитель
    перепроверяется, см. `require_web`.
    """

    csrf: str
    expires_at: datetime
    principal: Principal


class SessionRequest(BaseModel):
    token: str = Field(min_length=1, max_length=512)


router = APIRouter(include_in_schema=False)


def _store(request: Request) -> dict[str, WebSession]:
    sessions: dict[str, WebSession] = request.app.state.web_sessions
    return sessions


def _current(request: Request) -> WebSession | None:
    sid = request.cookies.get(SESSION_COOKIE)
    if not sid:
        return None
    store = _store(request)
    session = store.get(sid)
    if session is None:
        return None
    if session.expires_at <= utcnow():
        store.pop(sid, None)
        return None
    return session


async def require_local_host(request: Request) -> None:
    """Панель открывается только по заранее известному имени.

    Иначе имя вроде `evil.com`, отрезолвленное в 127.0.0.1, оказалось бы с
    панелью в одном origin и смогло бы прочитать метку CSRF.

    Адрес привязки доверенным именем не считается. Раньше при `0.0.0.0`
    проверка отключалась целиком — а это как раз штатный режим для проброса
    порта из Docker, то есть защита пропадала именно там, где нужна. Теперь
    петлевые имена разрешены всегда, конкретный адрес привязки — как есть, а
    всё остальное требует явного перечисления в `MAXUB_WEB_ALLOWED_HOSTS`.
    """
    settings = request.app.state.settings
    configured = str(settings.host).lower()
    allowed = set(LOOPBACK_HOSTS)
    if configured not in WILDCARD_HOSTS:
        allowed.add(configured)
    allowed.update(
        name.strip().lower() for name in str(settings.web_allowed_hosts).split(",") if name.strip()
    )
    if (request.url.hostname or "").lower() not in allowed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="панель доступна только по локальному адресу демона",
        )


async def secure_headers(response: Response) -> None:
    """Запрещает кэширование ответов панели и сужает права страницы."""
    response.headers.update(SECURITY_HEADERS)


def same_secret(provided: str, expected: str) -> bool:
    """Сравнение секретов, устойчивое к нелатинице.

    `compare_digest` на строках отказывается работать с не-ASCII, а в поле формы
    пользователь может вставить что угодно — сравниваем байты.
    """
    return secrets.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


async def require_web_session(request: Request) -> WebSession:
    """Пускает только с живой сессией, а изменяющие запросы — ещё и с CSRF."""
    session = _current(request)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="нужен вход")
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        provided = request.headers.get("x-csrf-token") or ""
        if not same_secret(provided, session.csrf):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="нет метки CSRF")
    return session


def require_web(*scopes: Scope) -> Callable[..., Awaitable[Principal]]:
    """Зависимость страницы: живая сессия, метка CSRF и нужные права.

    Панель — такой же клиент API, как CLI, и права у неё те же, что у токена,
    которым её открыли. Проверять их обязательно здесь, а не только на
    bearer-маршрутах: иначе токен с правом читать, введённый в форму входа,
    получил бы через панель и вход в аккаунт, и отправку.

    Предъявитель перечитывается на каждом запросе: отозванный токен закрывает
    сессию сразу, а урезанные права действуют с того же мгновения.
    """

    async def dependency(
        request: Request, session: WebSession = Depends(require_web_session)
    ) -> Principal:
        access: AccessControl = request.app.state.access
        principal = await access.refresh(session.principal)
        if principal is None:
            sid = request.cookies.get(SESSION_COOKIE)
            if sid:
                _store(request).pop(sid, None)
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="токен, которым открыта панель, отозван или просрочен",
            )
        session.principal = principal
        missing = principal.missing(scopes)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"токену не хватает прав: {', '.join(scope.value for scope in missing)}",
            )
        return principal

    return dependency


def issue_session(request: Request, response: Response, principal: Principal) -> WebSession:
    """Заводит сессию браузера и кладёт её идентификатор в cookie.

    Общее место для обоих входов — формы с токеном и одноразового кода из
    [web_handoff][maxub.api.routes.web_handoff]. Правила хранения cookie должны
    быть ровно одни: разъехавшись в мелочи вроде `SameSite` или `Path`, второй
    вход тихо ослабил бы первый.
    """
    store = _store(request)
    now = utcnow()
    for sid, item in list(store.items()):
        if item.expires_at <= now:
            del store[sid]
    while len(store) >= MAX_SESSIONS:
        del store[next(iter(store))]
    sid = secrets.token_urlsafe(32)
    session = WebSession(
        csrf=secrets.token_urlsafe(32), expires_at=now + SESSION_TTL, principal=principal
    )
    store[sid] = session
    response.set_cookie(
        SESSION_COOKIE,
        sid,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="strict",
        secure=request.url.scheme == "https",
        path="/web",
    )
    return session


@router.post("/session")
async def open_session(
    payload: SessionRequest, request: Request, response: Response
) -> dict[str, object]:
    """Меняет токен демона на сессию браузера.

    Вход по токену остаётся основным: панель открывают и из Docker, где нет ни
    лаунчера, ни одноразового кода, и токен там вводят руками.

    Панель открывается любым годным токеном, а не только корневым: права сессии
    равны правам этого токена, и владелец вправе открыть панель ограниченным
    токеном ровно для того, чтобы она умела меньше.

    Перебор токена смысла не имеет: это 256 случайных бит, а демон доступен
    только с петлевого интерфейса — отдельный счётчик попыток избыточен.
    """
    access: AccessControl = request.app.state.access
    principal = await access.authenticate(payload.token)
    if principal is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="неверный токен")
    session = issue_session(request, response, principal)
    return {"csrf": session.csrf, "scopes": format_scopes(principal.scopes)}


@router.get("/session")
async def session_info(request: Request) -> dict[str, object]:
    """Позволяет странице после перезагрузки узнать, что вход ещё жив.

    Права возвращаются вместе с меткой CSRF: страница по ним прячет то, чего
    этой сессии всё равно нельзя, — не как защиту (защита на сервере), а чтобы
    не предлагать кнопку, которая ответит отказом.
    """
    session = _current(request)
    if session is None:
        return {"authenticated": False, "csrf": None, "scopes": ""}
    # Предъявитель перепроверяется и здесь: иначе страница после отзыва токена
    # показывала бы вход живым и упиралась бы в отказ на первом же действии.
    access: AccessControl = request.app.state.access
    principal = await access.refresh(session.principal)
    if principal is None:
        sid = request.cookies.get(SESSION_COOKIE)
        if sid:
            _store(request).pop(sid, None)
        return {"authenticated": False, "csrf": None, "scopes": ""}
    session.principal = principal
    return {
        "authenticated": True,
        "csrf": session.csrf,
        "scopes": format_scopes(principal.scopes),
    }


@router.delete("/session", dependencies=[Depends(require_web_session)])
async def close_session(request: Request, response: Response) -> dict[str, str]:
    sid = request.cookies.get(SESSION_COOKIE)
    if sid:
        _store(request).pop(sid, None)
    response.delete_cookie(SESSION_COOKIE, path="/web")
    return {"status": "ok"}
