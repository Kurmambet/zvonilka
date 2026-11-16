"""Deterministic queue/lifecycle tests with a transport double, not WS/TCP tests."""

import asyncio
import unittest

from zvonilka.signaling.outbox import Outbox


class Sink:
    def __init__(self):
        self.messages: list[str] = []
        self.close_codes: list[int] = []
        self.started = asyncio.Event()
        self.sent = asyncio.Event()
        self.gate: asyncio.Event | None = None
        self.close_gate: asyncio.Event | None = None
        self.error: Exception | None = None
        self.close_error: Exception | None = None
        self.active = 0
        self.max_active = 0

    async def send_text(self, data: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.started.set()
        try:
            if self.gate is not None:
                await self.gate.wait()
            if self.error is not None:
                raise self.error
            self.messages.append(data)
            self.sent.set()
        finally:
            self.active -= 1

    async def close(self, code: int = 1000) -> None:
        self.close_codes.append(code)
        if self.close_gate is not None:
            await self.close_gate.wait()
        if self.close_error is not None:
            raise self.close_error


class OutboxTests(unittest.IsolatedAsyncioTestCase):
    async def test_fifo_and_single_writer(self):
        sink = Sink()
        async with Outbox(sink) as outbox:
            for message in ("first", "second", "third"):
                self.assertTrue(outbox.offer(message))
            async with asyncio.timeout(1):
                while len(sink.messages) != 3:
                    await sink.sent.wait()
                    sink.sent.clear()
            self.assertEqual(sink.messages, ["first", "second", "third"])
        self.assertEqual(sink.max_active, 1)
        self.assertEqual(sink.close_codes, [1000])
        self.assertTrue(outbox.closed)

    async def test_queue_overflow_is_fail_closed_and_nonblocking(self):
        sink = Sink()
        outbox = Outbox(sink, capacity=1)
        outbox.start()
        self.assertTrue(outbox.offer("first"))
        self.assertFalse(outbox.offer("second"))
        self.assertEqual(outbox.failure, "queue_full")
        self.assertEqual(outbox.pending, 0)
        self.assertFalse(outbox.offer("late"))
        await outbox.wait_closed()
        self.assertEqual(sink.messages, [])
        self.assertEqual(sink.close_codes, [1013])

    async def test_inflight_is_separate_from_bounded_pending_queue(self):
        sink = Sink()
        gate = asyncio.Event()
        sink.gate = gate
        outbox = Outbox(sink, capacity=1)
        outbox.start()
        outbox.offer("inflight")
        await asyncio.wait_for(sink.started.wait(), 1)
        self.assertTrue(outbox.offer("queued"))
        self.assertEqual(outbox.pending, 1)
        self.assertFalse(outbox.offer("overflow"))
        gate.set()
        await outbox.wait_closed()
        self.assertEqual(sink.messages, ["inflight"])
        self.assertEqual(sink.close_codes, [1013])

    async def test_slow_send_times_out_and_discards_stale_messages(self):
        sink = Sink()
        sink.gate = asyncio.Event()
        outbox = Outbox(sink, send_timeout=0.03)
        outbox.start()
        outbox.offer("blocked")
        outbox.offer("stale")
        await asyncio.wait_for(outbox.wait_closed(), 1)
        self.assertEqual(outbox.failure, "send_timeout")
        self.assertEqual(sink.messages, [])
        self.assertEqual(outbox.pending, 0)
        self.assertEqual(sink.close_codes, [1013])

    async def test_failed_send_is_sanitized(self):
        sink = Sink()
        sink.error = RuntimeError("secret-access-token")
        outbox = Outbox(sink)
        outbox.start()
        outbox.offer("message")
        await outbox.wait_closed()
        self.assertEqual(outbox.failure, "send_failed")
        self.assertEqual(sink.close_codes, [1011])
        self.assertNotIn("secret", str(outbox.failure))

    async def test_normal_close_discards_pending_and_sends_one_final_notice(self):
        sink = Sink()
        outbox = Outbox(sink)
        outbox.start()
        outbox.offer("obsolete")
        await outbox.aclose(1012, final_message="shutdown")
        self.assertEqual(sink.messages, ["shutdown"])
        self.assertEqual(sink.close_codes, [1012])
        self.assertFalse(outbox.offer("late"))
        await outbox.aclose()
        self.assertEqual(sink.close_codes, [1012])

    async def test_final_notice_is_bounded_too(self):
        sink = Sink()
        sink.gate = asyncio.Event()
        outbox = Outbox(sink, send_timeout=0.03)
        outbox.start()
        await asyncio.wait_for(outbox.aclose(1012, final_message="shutdown"), 1)
        self.assertEqual(outbox.failure, "send_timeout")
        self.assertEqual(sink.close_codes, [1012])
        self.assertTrue(outbox.closed)

    async def test_close_timeout_does_not_leave_writer_alive(self):
        sink = Sink()
        sink.close_gate = asyncio.Event()
        outbox = Outbox(sink, close_timeout=0.03)
        outbox.start()
        await asyncio.wait_for(outbox.aclose(), 1)
        self.assertEqual(outbox.failure, "close_timeout")
        self.assertTrue(outbox.closed)

    async def test_close_failure_is_recorded_without_exception_text(self):
        sink = Sink()
        sink.close_error = RuntimeError("secret")
        outbox = Outbox(sink)
        outbox.start()
        await outbox.aclose()
        self.assertEqual(outbox.failure, "close_failed")
        self.assertTrue(outbox.closed)

    async def test_utf8_limit_counts_bytes_and_rejects_before_enqueue(self):
        sink = Sink()
        outbox = Outbox(sink, max_message_bytes=4)
        outbox.start()
        self.assertTrue(outbox.offer("яя"))
        with self.assertRaises(ValueError):
            outbox.offer("яяя")
        with self.assertRaises(ValueError):
            outbox.offer("\ud800")
        self.assertEqual(outbox.pending, 1)
        await outbox.aclose()

    async def test_failed_final_notice_validation_does_not_change_state(self):
        sink = Sink()
        outbox = Outbox(sink, max_message_bytes=4)
        outbox.start()
        with self.assertRaises(ValueError):
            outbox.request_close(final_message="too long")
        self.assertFalse(outbox.closing)
        await outbox.aclose()

    async def test_start_and_offer_lifecycle_guards(self):
        sink = Sink()
        outbox = Outbox(sink)
        with self.assertRaises(RuntimeError):
            outbox.offer("early")
        with self.assertRaises(RuntimeError):
            await outbox.wait_closed()
        outbox.start()
        with self.assertRaises(RuntimeError):
            outbox.start()
        await outbox.aclose()
        with self.assertRaises(RuntimeError):
            outbox.start()

    async def test_cancelled_waiter_does_not_cancel_writer(self):
        sink = Sink()
        outbox = Outbox(sink)
        outbox.start()
        waiter = asyncio.create_task(outbox.wait_closed())
        await asyncio.sleep(0)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertTrue(outbox.offer("still alive"))
        await outbox.aclose()
        self.assertTrue(outbox.closed)

    async def test_no_tasks_remain_after_context_exit(self):
        baseline = set(asyncio.all_tasks())
        sink = Sink()
        async with Outbox(sink):
            pass
        await asyncio.sleep(0)
        self.assertEqual(set(asyncio.all_tasks()), baseline)

    async def test_slow_recipient_does_not_block_fast_recipient(self):
        slow = Sink()
        slow.gate = asyncio.Event()
        fast = Sink()
        async with Outbox(slow, send_timeout=0.1) as slow_box, Outbox(fast) as fast_box:
            slow_box.offer("slow")
            await asyncio.wait_for(slow.started.wait(), 1)
            fast_box.offer("fast")
            await asyncio.wait_for(fast.sent.wait(), 0.5)
            self.assertEqual(fast.messages, ["fast"])
            self.assertEqual(slow.messages, [])

    async def test_cancellation_during_final_notice_still_closes_transport(self):
        sink = Sink()
        sink.gate = asyncio.Event()
        outbox = Outbox(sink)
        outbox.start()
        outbox.request_close(1012, final_message="shutdown")
        await asyncio.wait_for(sink.started.wait(), 1)
        task = outbox._task
        assert task is not None
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await outbox.wait_closed()
        self.assertEqual(sink.close_codes, [1012])
        self.assertTrue(outbox.closed)

    async def test_invalid_configuration(self):
        sink = Sink()
        with self.assertRaises(ValueError):
            Outbox(sink, capacity=0)
        with self.assertRaises(ValueError):
            Outbox(sink, capacity=True)
        with self.assertRaises(ValueError):
            Outbox(sink, max_message_bytes=0)
        with self.assertRaises(ValueError):
            Outbox(sink, send_timeout=float("inf"))
        with self.assertRaises(ValueError):
            Outbox(sink, close_timeout=0)


if __name__ == "__main__":
    unittest.main()
