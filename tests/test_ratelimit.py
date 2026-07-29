"""Тесты ограничителя частоты.

Лимит — это не украшение, а единственное, что стоит между автоматизацией и
блокировкой аккаунта. Поэтому проверяется не «функция вызывается», а поведение:
сколько действий проходит за отведённое время, что делает второй параллельный
вызов и переживает ли штраф сервера перезапуск.

Время здесь настоящее, а не выдуманное: ограничитель спит `asyncio.sleep`, и
подменять сон значило бы проверять подмену, а не ограничитель. Чтобы тесты
оставались быстрыми, взяты частоты в сотни раз выше боевых — период пополнения
получается около 0.1 с. Допуски заданы с запасом на планировщик: проверяется
порядок величины и соотношение, а не микросекунды.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from maxub.core.models import utcnow
from maxub.core.ratelimit import RateLimiter, TokenBucket

# 600 действий в минуту — это 0.1 с на действие: достаточно медленно, чтобы
# отличить ожидание от его отсутствия, и достаточно быстро для набора тестов.
FAST_RATE = 600.0
PERIOD = 0.1
TOLERANCE = 0.06


class Stopwatch:
    """Замер по часам цикла событий — тем же, по которым живёт token bucket."""

    def __init__(self) -> None:
        self._started = asyncio.get_running_loop().time()

    @property
    def elapsed(self) -> float:
        return asyncio.get_running_loop().time() - self._started


async def test_burst_passes_without_waiting() -> None:
    """Запас в ведре тратится сразу — иначе первое же действие ждало бы зря."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=3, jitter_seconds=0.0)
    watch = Stopwatch()

    for _ in range(3):
        await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(0.0, abs=TOLERANCE)


async def test_action_after_burst_waits_for_refill() -> None:
    """Исчерпав запас, следующее действие ждёт период пополнения."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=3, jitter_seconds=0.0)
    for _ in range(3):
        await limiter.acquire(1, "send_text")
    watch = Stopwatch()

    await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(PERIOD, abs=TOLERANCE)


async def test_rate_holds_over_a_long_run() -> None:
    """Установившийся ритм держится на длинной дистанции, а не только на первых
    действиях.

    Всплеск и первое ожидание проверяются выше поштучно; здесь важно, что
    последовательность из десяти действий занимает десять периодов — ни быстрее
    (значит, лимит где-то обходится), ни заметно медленнее (значит, ожидания
    накладываются друг на друга и демон работает вдвое медленнее заявленного).
    """
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=1, jitter_seconds=0.0)
    await limiter.acquire(1, "send_text")
    watch = Stopwatch()

    for _ in range(10):
        await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(10 * PERIOD, abs=0.25)


async def test_parallel_callers_do_not_share_one_token() -> None:
    """Два одновременных вызова на один ключ не проходят оба.

    Без замка обе корутины видят свободный токен и списывают его каждая: лимит
    обходится ровно в тот момент, когда действий больше всего.
    """
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=1, jitter_seconds=0.0)
    watch = Stopwatch()

    await asyncio.gather(*(limiter.acquire(1, "send_text") for _ in range(3)))

    assert watch.elapsed == pytest.approx(2 * PERIOD, abs=TOLERANCE)


async def test_accounts_and_actions_do_not_share_a_bucket() -> None:
    """Ведро своё на пару (аккаунт, действие) — общий лимит скрывал бы виновника."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=1, jitter_seconds=0.0)
    watch = Stopwatch()

    await limiter.acquire(1, "send_text")
    await limiter.acquire(2, "send_text")
    await limiter.acquire(1, "fetch_history")

    assert watch.elapsed == pytest.approx(0.0, abs=TOLERANCE)


async def test_penalty_from_server_outranks_our_limit() -> None:
    """`retry_after` сервера важнее собственного расчёта — ведро тут свободно."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE * 10, burst=100, jitter_seconds=0.0)
    limiter.penalize(1, "send_text", retry_after=0.3)
    watch = Stopwatch()

    await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(0.3, abs=TOLERANCE)


async def test_penalty_is_served_once() -> None:
    """Отсиженный штраф снимается — иначе аккаунт стоял бы вечно."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE * 10, burst=100, jitter_seconds=0.0)
    limiter.penalize(1, "send_text", retry_after=0.2)
    await limiter.acquire(1, "send_text")
    watch = Stopwatch()

    await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(0.0, abs=TOLERANCE)


def test_penalty_extends_but_never_shortens() -> None:
    """Второй, более мягкий ответ сервера не отменяет уже назначенный штраф."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=1, jitter_seconds=0.0)

    long_until = limiter.penalize(1, "send_text", retry_after=600.0)
    short_until = limiter.penalize(1, "send_text", retry_after=1.0)

    assert short_until == long_until


def test_restore_ignores_expired_penalty() -> None:
    """Просроченный штраф из базы не оживает при старте демона."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE, burst=1, jitter_seconds=0.0)

    limiter.restore(1, "send_text", utcnow() - timedelta(seconds=1))

    assert limiter.penalty_until(1, "send_text") is None


async def test_restored_penalty_delays_the_next_action() -> None:
    """А непросроченный — переживает перезапуск и продолжает действовать."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE * 10, burst=100, jitter_seconds=0.0)
    limiter.restore(1, "send_text", utcnow() + timedelta(seconds=0.3))
    watch = Stopwatch()

    await limiter.acquire(1, "send_text")

    assert watch.elapsed == pytest.approx(0.3, abs=TOLERANCE)


async def test_penalty_extended_while_waiting_is_served_to_the_end() -> None:
    """Продление штрафа во время ожидания досиживается, а не теряется.

    Сервер может ответить `retry_after` второй раз, пока первый вызов ещё спит.
    Если проснувшийся вызов уходит вперёд по старому расчёту, аккаунт получает
    ровно то, от чего штраф защищает.
    """
    limiter = RateLimiter(rate_per_minute=FAST_RATE * 10, burst=100, jitter_seconds=0.0)
    limiter.penalize(1, "send_text", retry_after=0.1)
    watch = Stopwatch()
    waiting = asyncio.ensure_future(limiter.acquire(1, "send_text"))
    await asyncio.sleep(0.05)

    limiter.penalize(1, "send_text", retry_after=0.35)
    await waiting

    assert watch.elapsed == pytest.approx(0.4, abs=TOLERANCE)


async def test_jitter_stays_inside_its_bounds() -> None:
    """Джиттер добавляет не больше заявленного: это разброс, а не второй лимит."""
    limiter = RateLimiter(rate_per_minute=FAST_RATE * 10, burst=100, jitter_seconds=0.02)
    watch = Stopwatch()

    for _ in range(20):
        await limiter.acquire(1, "send_text")

    assert 0.0 < watch.elapsed <= 20 * 0.02 + TOLERANCE


async def test_bucket_never_refills_above_burst() -> None:
    """Простой не копит запас сверх заявленного всплеска.

    Иначе демон, простоявший ночь, проснулся бы с правом на сотни действий
    подряд — самый заметный для сервера способ себя выдать.
    """
    bucket = TokenBucket(rate_per_minute=FAST_RATE, burst=2)
    bucket.consume()
    bucket.consume()
    await asyncio.sleep(10 * PERIOD)

    assert bucket.delay_for_next() == 0.0
    bucket.consume()
    assert bucket.delay_for_next() == 0.0
    bucket.consume()
    assert bucket.delay_for_next() > 0.0
