"""Bind one Outbox writer to the lifecycle of one accepted connection."""

import asyncio

from .outbox import Outbox, TextSink
from .service import Connection, Rejected


class SocketWriter:
    def __init__(
        self,
        sink: TextSink,
        connection: Connection,
        *,
        capacity: int = 32,
        max_message_bytes: int = 16384,
        send_timeout: float = 2.0,
        close_timeout: float = 2.0,
    ):
        self.connection = connection
        self.outbox = Outbox(
            sink,
            capacity=capacity,
            max_message_bytes=max_message_bytes,
            send_timeout=send_timeout,
            close_timeout=close_timeout,
        )
        self._monitor: asyncio.Task[None] | None = None

    def start(self) -> None:
        self.outbox.start()
        self._monitor = asyncio.create_task(self._watch(), name="zvonilka-ws-writer-watch")

    async def _watch(self) -> None:
        try:
            await self.outbox.wait_closed()
        except asyncio.CancelledError:
            self.connection.stop("internal_error")
            raise
        except Exception:  # noqa: BLE001 -- Signal failure without leaking transport details.
            self.connection.stop("internal_error")
        else:
            if self.outbox.failure in {"queue_full", "send_timeout"}:
                self.connection.stop("overloaded")
            elif self.outbox.failure is not None:
                self.connection.stop("internal_error")

    def send(self, message: str) -> None:
        self.connection.check()
        if not self.outbox.offer(message):
            raise Rejected("overloaded", 1013)

    def request_close(self, code: int = 1000, *, final_message: str | None = None) -> None:
        self.outbox.request_close(code, final_message=final_message)

    async def aclose(self) -> None:
        self.outbox.request_close()
        try:
            await self.outbox.wait_closed()
        finally:
            monitor = self._monitor
            if monitor is not None:
                await asyncio.shield(monitor)
