"""Client-side rate limiter: at most N calls in flight and M calls per rolling minute."""

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable

WINDOW_S = 60.0


class RateLimiter:
    """Use as ``async with limiter:`` around each provider request (retries included).

    A requests_per_minute of 0 or less disables the window check.
    """

    def __init__(
        self,
        max_concurrent: int,
        requests_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._slots = asyncio.Semaphore(max(1, max_concurrent))
        self._per_minute = requests_per_minute
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> "RateLimiter":
        await self._slots.acquire()
        try:
            await self._wait_for_window()
        except BaseException:
            self._slots.release()
            raise
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        self._slots.release()

    async def _wait_for_window(self) -> None:
        if self._per_minute <= 0:
            return
        async with self._lock:
            while True:
                now = self._clock()
                while self._starts and now - self._starts[0] >= WINDOW_S:
                    self._starts.popleft()
                if len(self._starts) < self._per_minute:
                    self._starts.append(now)
                    return
                await self._sleep(self._starts[0] + WINDOW_S - now)
