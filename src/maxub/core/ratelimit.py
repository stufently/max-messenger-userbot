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
import random
from dataclasses import dataclass, field


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
        self._penalty: dict[tuple[int, str], float] = {}

    def _bucket(self, account_id: int, action: str) -> TokenBucket:
        key = (account_id, action)
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = TokenBucket(rate_per_minute=self._rate, burst=self._burst)
            self._buckets[key] = bucket
        return bucket

    def penalize(self, account_id: int, action: str, retry_after: float) -> None:
        """Учитывает ``retry_after`` от сервера — он всегда важнее нашего лимита."""
        loop_now = asyncio.get_running_loop().time()
        key = (account_id, action)
        self._penalty[key] = max(self._penalty.get(key, 0.0), loop_now + retry_after)

    async def acquire(self, account_id: int, action: str) -> None:
        key = (account_id, action)
        penalty_until = self._penalty.get(key)
        if penalty_until is not None:
            remaining = penalty_until - asyncio.get_running_loop().time()
            if remaining > 0:
                await asyncio.sleep(remaining)
            self._penalty.pop(key, None)

        bucket = self._bucket(account_id, action)
        delay = bucket.delay_for_next()
        if delay > 0:
            await asyncio.sleep(delay)
        if self._jitter > 0:
            await asyncio.sleep(random.uniform(0, self._jitter))
        bucket.consume()
