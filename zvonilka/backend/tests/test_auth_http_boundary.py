import json
import unittest
from uuid import UUID

from zvonilka.http_boundary import SafeHttpBoundary


async def request(app, chunks):
    sent = []
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive():
        return messages.pop(0) if messages else {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    await app({"type": "http", "state": {}}, receive, send)
    return sent


class BoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunked_body_limit_enforced_before_app(self):
        reached = []

        async def inner(scope, receive, send):
            reached.append(True)

        sent = await request(SafeHttpBoundary(inner, max_body=4), [b"abc", b"de"])
        self.assertFalse(reached)
        self.assertEqual(sent[0]["status"], 413)
        self.assertEqual(json.loads(sent[1]["body"])["error"]["code"], "request_too_large")

    async def test_unknown_exception_has_safe_json(self):
        async def broken(scope, receive, send):
            raise RuntimeError("password=TOP_SECRET")

        with self.assertLogs("zvonilka.http", level="ERROR"):
            sent = await request(SafeHttpBoundary(broken), [b"{}"])
        self.assertEqual(sent[0]["status"], 500)
        self.assertNotIn(b"TOP_SECRET", sent[1]["body"])
        data = json.loads(sent[1]["body"])
        UUID(data["error"]["request_id"])

    async def test_body_replayed_and_headers_protected(self):
        seen = []

        async def inner(scope, receive, send):
            seen.append(await receive())
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"cache-control", b"public"), (b"x-request-id", b"untrusted")],
                }
            )
            await send({"type": "http.response.body", "body": b"{}"})

        sent = await request(SafeHttpBoundary(inner), [b"{", b"}"])
        self.assertEqual(seen[0]["body"], b"{}")
        headers = dict(sent[0]["headers"])
        self.assertEqual(headers[b"cache-control"], b"no-store")
        UUID(headers[b"x-request-id"].decode())
        self.assertNotEqual(headers[b"x-request-id"], b"untrusted")

    async def test_framework_error_already_sent_is_not_sent_twice(self):
        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 500, "headers": []})
            await send({"type": "http.response.body", "body": b"{}"})
            raise RuntimeError("framework re-raises after sending error")

        with self.assertLogs("zvonilka.http", level="ERROR"):
            sent = await request(SafeHttpBoundary(inner), [b""])
        self.assertEqual(sum(m["type"] == "http.response.start" for m in sent), 1)

    async def test_broken_stream_is_not_falsely_reported_as_complete(self):
        async def inner(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            raise RuntimeError("stream failed")

        with self.assertLogs("zvonilka.http", level="ERROR"), self.assertRaises(RuntimeError):
            await request(SafeHttpBoundary(inner), [b""])
