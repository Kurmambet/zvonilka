"""ASGI HTTP boundary: bounded bodies, generated request IDs, no raw exception text."""

import json
import logging
from uuid import uuid4

logger = logging.getLogger("zvonilka.http")


class SafeHttpBoundary:
    def __init__(self, app, *, max_body: int = 16384):
        self.app, self.max_body = app, max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request_id = str(uuid4())
        scope = {**scope, "state": {**scope.get("state", {}), "request_id": request_id}}

        async def respond(status, code):
            body = json.dumps({"error": {"code": code, "request_id": request_id}}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"cache-control", b"no-store"),
                        (b"x-request-id", request_id.encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})

        # Entire auth request bodies are small; reject before framework parsing.
        chunks, size = [], 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] != "http.request":
                continue
            chunk = message.get("body", b"")
            size += len(chunk)
            if size > self.max_body:
                await respond(413, "request_too_large")
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        body = b"".join(chunks)
        delivered = False
        started = False
        finished = False

        async def replay_receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        async def guarded_send(message):
            nonlocal started, finished
            if message["type"] == "http.response.start":
                started = True
                message = {
                    **message,
                    "headers": [
                        (k, v)
                        for k, v in message.get("headers", [])
                        if k.lower() not in (b"x-request-id", b"cache-control")
                    ]
                    + [(b"x-request-id", request_id.encode()), (b"cache-control", b"no-store")],
                }
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body", False):
                finished = True

        try:
            await self.app(scope, replay_receive, guarded_send)
        except Exception:
            logger.exception(
                "",
                extra={
                    "safe_event": "unexpected_error",
                    "safe_fields": {"code": "internal_error", "request_id": request_id},
                },
            )

            if not started:
                await respond(500, "internal_error")
            # If already started, re-raise: closing a broken response is safer
            # than silently claiming success. Safe root logging omits raw traceback.
            elif not finished:
                raise
