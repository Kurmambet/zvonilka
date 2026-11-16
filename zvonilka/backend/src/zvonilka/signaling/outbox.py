"""Bounded single-writer transport primitive; not yet wired into the WS router."""

from __future__ import annotations

import asyncio
import math
from typing import Literal, Protocol, Self

Failure = Literal["queue_full", "send_timeout", "send_failed", "close_timeout", "close_failed"]


class TextSink(Protocol):
    async def send_text(self, data: str) -> None: ...
    async def close(self, code: int = 1000) -> None: ...


class Outbox:
    """offer() means queued, not delivered. Closing discards queued messages."""

    def __init__(
        self,
        sink: TextSink,
        *,
        capacity: int = 32,
        max_message_bytes: int = 16384,
        send_timeout: float = 2.0,
        close_timeout: float = 2.0,
    ):
        for value in (capacity, max_message_bytes):
            if type(value) is not int or value < 1:
                raise ValueError("queue limits must be positive integers")
        for value in (send_timeout, close_timeout):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("timeouts must be finite and positive")
        self._sink = sink
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=capacity)
        self._max_message_bytes = max_message_bytes
        self._send_timeout = send_timeout
        self._close_timeout = close_timeout
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._closing = False
        self._closed = False
        self._close_code = 1000
        self._final_message: str | None = None
        self._failure: Failure | None = None

    @property
    def pending(self) -> int:
        return self._queue.qsize()

    @property
    def closing(self) -> bool:
        return self._closing

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def failure(self) -> Failure | None:
        return self._failure

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("outbox already started")
        if self._closing:
            raise RuntimeError("outbox is closing")
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._run(), name="zvonilka-ws-outbox")

    def _validate(self, message: str) -> None:
        if not isinstance(message, str):
            raise TypeError("outbound message must be text")
        try:
            size = len(message.encode("utf-8"))
        except UnicodeError:
            raise ValueError("outbound message is not valid UTF-8") from None
        if size > self._max_message_bytes:
            raise ValueError("outbound message exceeds limit")

    def _discard(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except asyncio.QueueEmpty:
                break

    def offer(self, message: str) -> bool:
        if self._task is None:
            raise RuntimeError("outbox is not started")
        if self._closing:
            return False
        self._validate(message)
        try:
            self._queue.put_nowait(message)
        except asyncio.QueueFull:
            self._fail("queue_full", 1013)
            return False
        self._wake.set()
        return True

    def request_close(self, code: int = 1000, *, final_message: str | None = None) -> None:
        if self._task is None:
            raise RuntimeError("outbox is not started")
        if self._closing:
            return
        if final_message is not None:
            self._validate(final_message)
        self._closing = True
        self._close_code = code
        self._final_message = final_message
        self._discard()
        self._wake.set()

    def _fail(self, failure: Failure, code: int) -> None:
        if self._failure is None:
            self._failure = failure
        if not self._closing:
            self.request_close(code)

    async def _send(self, message: str) -> bool:
        try:
            async with asyncio.timeout(self._send_timeout):
                await self._sink.send_text(message)
        except TimeoutError:
            self._fail("send_timeout", 1013)
            return False
        except Exception:  # noqa: BLE001 -- Transport boundary; retain no exception text.
            self._fail("send_failed", 1011)
            return False
        return True

    async def _run(self) -> None:
        cancelled = False
        try:
            while not self._closing:
                await self._wake.wait()
                self._wake.clear()
                while not self._closing:
                    try:
                        message = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    try:
                        sent = await self._send(message)
                    finally:
                        self._queue.task_done()
                    if not sent:
                        break
        except asyncio.CancelledError:
            cancelled = True
            if not self._closing:
                self._close_code = 1001
            raise
        finally:
            self._closing = True
            self._discard()
            try:
                if not cancelled and self._final_message is not None and self._failure is None:
                    await self._send(self._final_message)
            finally:
                try:
                    async with asyncio.timeout(self._close_timeout):
                        await self._sink.close(code=self._close_code)
                except TimeoutError:
                    self._fail("close_timeout", self._close_code)
                except Exception:  # noqa: BLE001 -- Never expose transport exception text.
                    self._fail("close_failed", self._close_code)
                finally:
                    self._final_message = None
                    self._closed = True

    async def wait_closed(self) -> None:
        task = self._task
        if task is None:
            raise RuntimeError("outbox is not started")
        await asyncio.shield(task)

    async def aclose(self, code: int = 1000, *, final_message: str | None = None) -> None:
        self.request_close(code, final_message=final_message)
        await self.wait_closed()

    async def __aenter__(self) -> Self:
        self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.aclose()
