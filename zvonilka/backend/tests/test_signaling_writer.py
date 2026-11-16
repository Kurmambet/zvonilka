"""Actual Outbox binding and stop propagation; transport is a dependency double."""

import asyncio
import unittest

from zvonilka.signaling.sender import SocketWriter
from zvonilka.signaling.service import Connection, Rejected


class Sink:
    def __init__(self):
        self.sent: list[str] = []
        self.codes: list[int] = []
        self.error: Exception | None = None
        self.gate: asyncio.Event | None = None
        self.sent_event = asyncio.Event()
        self.owners: set[object] = set()

    async def send_text(self, data: str) -> None:
        self.owners.add(asyncio.current_task())
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        self.sent.append(data)
        self.sent_event.set()

    async def close(self, code: int = 1000) -> None:
        self.owners.add(asyncio.current_task())
        self.codes.append(code)


class SocketWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_terminal_notice_use_one_task(self):
        sink = Sink()
        connection = Connection()
        writer = SocketWriter(sink, connection)
        writer.start()
        writer.send("joined")
        await asyncio.wait_for(sink.sent_event.wait(), 1)
        writer.request_close(1012, final_message="shutdown")
        await writer.aclose()
        self.assertEqual(sink.sent, ["joined", "shutdown"])
        self.assertEqual(sink.codes, [1012])
        self.assertEqual(len(sink.owners), 1)
        self.assertFalse(connection.stopped.is_set())

    async def test_transport_failure_wakes_connection(self):
        sink = Sink()
        sink.error = OSError("secret")
        connection = Connection()
        writer = SocketWriter(sink, connection)
        writer.start()
        writer.send("joined")
        await asyncio.wait_for(connection.stopped.wait(), 1)
        self.assertEqual(connection.reason, "internal_error")
        await writer.aclose()
        self.assertEqual(sink.codes, [1011])

    async def test_timeout_wakes_connection(self):
        sink = Sink()
        sink.gate = asyncio.Event()
        connection = Connection()
        writer = SocketWriter(sink, connection, send_timeout=0.03)
        writer.start()
        writer.send("blocked")
        await asyncio.wait_for(connection.stopped.wait(), 1)
        self.assertEqual(connection.reason, "overloaded")
        await writer.aclose()
        self.assertEqual(sink.codes, [1013])

    async def test_overflow_fails_without_waiting_for_writer(self):
        sink = Sink()
        connection = Connection()
        writer = SocketWriter(sink, connection, capacity=1)
        writer.start()
        writer.send("first")
        with self.assertRaises(Rejected) as caught:
            writer.send("overflow")
        self.assertEqual(caught.exception.code, "overloaded")
        await writer.aclose()
        self.assertEqual(sink.codes, [1013])
        self.assertEqual(connection.reason, "overloaded")

    async def test_replaced_connection_cannot_enqueue(self):
        sink = Sink()
        connection = Connection()
        writer = SocketWriter(sink, connection)
        writer.start()
        connection.stop("connection_replaced")
        with self.assertRaises(Rejected) as caught:
            writer.send("stale")
        self.assertEqual(caught.exception.code, "connection_replaced")
        await writer.aclose()
        self.assertEqual(sink.sent, [])

    async def test_close_reaps_writer_and_monitor(self):
        baseline = set(asyncio.all_tasks())
        writer = SocketWriter(Sink(), Connection())
        writer.start()
        await writer.aclose()
        await writer.aclose()
        await asyncio.sleep(0)
        self.assertEqual(set(asyncio.all_tasks()), baseline)


if __name__ == "__main__":
    unittest.main()
