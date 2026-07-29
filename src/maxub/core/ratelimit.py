"""Ограничение частоты действий.

Token bucket плюс случайный jitter. Ведро своё на каждую пару
(аккаунт, тип действия) — общий лимит на процесс скрывал бы, какой именно
аккаунт упирается в потолок.

Важно: подобранных «безопасных» констант для закрытого API не существует.
Здесь реализуется механика, а конкретные значения задаются в настройках и
уточняются практикой.
"""

from __future__ import annotations

import asyncio
import math
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from maxub.core.models import utcnow


@dataclass
class TokenBucket:
    rate_per_minute: float
    burst: int
    _tokens: float = field(init=False)
    _updated: float = field(init=False)

    def __post_init__(self) -> None:
        self._tokens = float(self.burst)
        self._updated = asyncio.get_running_loop().time()

    def _refill(self, now: float) -> None:
        elapsed = now - self._updated
        self._updated = now
        self._tokens = min(
            float(self.burst),
            self._tokens + elapsed * (self.rate_per_minute / 60.0),
        )

    def delay_for_next(self) -> float:
        """Сколько секунд ждать до следующего разрешённого действия."""
        now = asyncio.get_running_loop().time()
        self._refill(now)
        if self._tokens >= 1.0:
            return 0.0
        missing = 1.0 - self._tokens
        return missing / (self.rate_per_minute / 60.0)

    def consume(self) -> None:
        self._tokens -= 1.0


class RateLimiter:
    """Реестр вёдер по ключу ``(account_id, action)``."""

    def __init__(self, rate_per_minute: float, burst: int, jitter_seconds: float) -> None:
        self._rate = rate_per_minute
        self._burst = burst
        self._jitter = jitter_seconds
        self._buckets: dict[tuple[int, str], TokenBucket] = {}
        self._penalty: dict[tuple[int, str], datetime] = {}
        self._locks: dict[tuple[int, str], asyncio.Lock] = {}

    def _bucket(self, key: tuple[int, str]) -> TokenBucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(rate_per_minute=self._rate, burst=self._burst)
            self._buckets[key] = bucket
        return bucket

    def _lock(self, key: tuple[int, str]) -> asyncio.Lock:
        """Замок на ключ: разрешение выдаётся по одному.

        Без него два одновременных вызова на одно ведро оба видят свободный
        токен, оба спят и оба списывают — лимит обходится ровно в момент, когда
        он нужнее всего. Сегодня потребитель один и последовательный, но лимит —
        это защита аккаунта от блокировки, и держаться она должна на замке, а не
        на текущем расположении вызовов.
        """
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def penalize(self, account_id: int, action: str, retry_after: float) -> datetime:
        """Учитывает ``retry_after`` от сервера — он всегда важнее нашего лимита.

        Момент возврата хранится по стенным часам, а не по времени цикла: его
        нужно сохранять в БД, чтобы штраф пережил перезапуск демона.
        """
        key = (account_id, action)
        until = utcnow() + timedelta(seconds=retry_after)
        current = self._penalty.get(key)
        self._penalty[key] = max(current, until) if current else until
        return self._penalty[key]

    def penalty_until(self, account_id: int, action: str) -> datetime | None:
        """Действующий штраф по ключу — ``None``, если его нет."""
        return self._penalty.get((account_id, action))

    def restore(self, account_id: int, action: str, until: datetime) -> None:
        """Возвращает штраф, сохранённый до перезапуска."""
        if until > utcnow():
            self._penalty[(account_id, action)] = until

    async def acquire(self, account_id: int, action: str) -> None:
        """Берёт разрешение, сколько бы ни пришлось ждать."""
        await self.try_acquire(account_id, action, max_wait=math.inf)

    async def try_acquire(self, account_id: int, action: str, max_wait: float) -> datetime | None:
        """Берёт разрешение, если ждать не дольше ``max_wait``.

        Возвращает ``None``, когда разрешение взято, и момент, когда его можно
        будет взять, когда ждать пришлось бы дольше дозволенного — тогда ничего
        не расходуется и никто не задерживается.

        Решение и его исполнение происходят под одним замком, и это главное
        свойство метода. Раздельная пара «спросить оценку — потом взять»
        оставляла бы окно, в которое штраф успевает продлиться: вызывающий
        решает «подожду десять секунд», а ждёт час, и общий воркер стоит всё это
        время. Такого окна здесь нет по построению.
        """
        key = (account_id, action)
        async with self._lock(key):
            wait = self._wait_estimate(key)
            if wait > max_wait:
                return utcnow() + timedelta(seconds=wait)
            await self._serve_penalty(key)
            bucket = self._bucket(key)
            # Ожидание в цикле, а не однократное: после сна ведро пересчитывается
            # по часам, а не списывается вслепую. На установившемся ритме разницы
            # нет — слепое списание уводит баланс в минус ровно на столько, на
            # сколько следующий пересчёт его вернёт. Разница появляется там, где
            # сон оказался длиннее рассчитанного: под нагрузкой цикл событий
            # будит корутину позже, и списывать по устаревшему расчёту значит
            # накапливать долг, которого не было.
            while True:
                delay = bucket.delay_for_next()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            if self._jitter > 0:
                await asyncio.sleep(random.uniform(0, self._jitter))
            bucket.consume()
            return None

    def _wait_estimate(self, key: tuple[int, str]) -> float:
        """Сколько секунд заняло бы разрешение, если брать его прямо сейчас.

        Из двух ожиданий берётся большее, а не их сумма: они идут параллельно.
        Ведро пополняется по часам и на время штрафа не замирает, поэтому к концу
        штрафа токен уже накопился. Сумма завысила бы оценку и уводила бы в
        отложенные записи те, которым хватило бы короткого ожидания на месте.

        Джиттер в оценку не входит: он мал по сравнению с порогом и добавляется
        уже после решения, а его учёт сделал бы оценку случайной величиной.
        """
        wait = 0.0
        penalty_until = self._penalty.get(key)
        if penalty_until is not None:
            wait = max(0.0, (penalty_until - utcnow()).total_seconds())
        return max(wait, self._bucket(key).delay_for_next())

    async def _serve_penalty(self, key: tuple[int, str]) -> None:
        """Досиживает штраф, назначенный сервером.

        Штраф мог быть продлён, пока мы спали, поэтому проверка повторяется до
        тех пор, пока времени не останется.
        """
        while True:
            penalty_until = self._penalty.get(key)
            if penalty_until is None:
                return
            remaining = (penalty_until - utcnow()).total_seconds()
            if remaining <= 0:
                self._penalty.pop(key, None)
                return
            await asyncio.sleep(remaining)
