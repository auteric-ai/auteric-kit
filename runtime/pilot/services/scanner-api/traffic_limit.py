"""Optional per-batch request pacing inherited by scanner child coroutines."""
import asyncio
import contextvars
import time

request_limiter = contextvars.ContextVar('scanner_request_limiter', default=None)


class RequestLimiter:
    def __init__(self, per_second):
        self.interval = 1 / per_second
        self.lock = asyncio.Lock()
        self.next = 0.0

    async def wait(self):
        async with self.lock:
            await asyncio.sleep(max(0, self.next - time.monotonic()))
            self.next = time.monotonic() + self.interval
