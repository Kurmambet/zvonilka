import asyncio
import json
import os
import secrets
import sys
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

# BASE = "http://127.0.0.1:8000"
BASE = os.environ.get("ZVONILKA_SMOKE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
WS_BASE = BASE.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
MODE = sys.argv[1] if len(sys.argv) == 2 else "rooms"


def check(ok, label):
    if not ok:
        raise RuntimeError(label)
    print("OK", label, flush=True)


async def http(client, method, path, status, token=None, body=None) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    result = await client.request(method, path, headers=headers, json=body)
    check(
        result.status_code == status,
        f"{method} {path.split('/')[3]}: expected {status}, got {result.status_code}",
    )
    if status == 204:
        check(not result.content, "HTTP 204 has an empty body")
        return {}

    payload = result.json()
    if not isinstance(payload, dict):
        raise TypeError("Expected a JSON object in the HTTP response")
    return payload


async def recv(ws, kind, **fields):
    msg = json.loads(await asyncio.wait_for(ws.recv(), 5))
    check(
        msg.get("type") == kind and all(msg.get(k) == v for k, v in fields.items()),
        f"expected {kind} {fields}; got {msg.get('type')} / {msg.get('code')}",
    )
    return msg


async def send(ws, kind, **fields):
    rid = str(uuid4())
    await ws.send(json.dumps(dict(version=1, type=kind, request_id=rid, **fields)))
    return rid


async def closed(ws):
    try:
        await asyncio.wait_for(ws.recv(), 5)
    except ConnectionClosed as exc:
        print("OK WS closed:", exc.rcvd.code if exc.rcvd else "no close frame", flush=True)
    else:
        raise RuntimeError("Expected WS close")


async def main():
    if MODE not in {"rooms", "revoke", "expiry", "shutdown"}:
        raise SystemExit("Mode: rooms | revoke | expiry | shutdown")
    async with httpx.AsyncClient(base_url=BASE, timeout=5) as client:

        async def account():
            data = {"username": "sm" + uuid4().hex[:12], "password": secrets.token_urlsafe(24)}
            await http(
                client, "POST", "/api/v1/auth/register", 201, body=dict(data, display_name="Smoke")
            )
            return await http(client, "POST", "/api/v1/auth/login", 200, body=data)

        a, b = await account(), await account()
        at, bt = a["access_token"], b["access_token"]
        room = (await http(client, "POST", "/api/v1/rooms", 201, at, {}))["room_id"]
        path = f"/api/v1/rooms/{room}"
        # url = "ws://127.0.0.1:8000" + path + "/ws"
        url = f"{WS_BASE}{path}/ws"
        invite = (await http(client, "POST", path + "/invites", 201, at))["invitation"]
        await http(client, "POST", "/api/v1/invites/redeem", 200, bt, {"invitation": invite})

        async def authenticate(ws, token):
            await send(
                ws,
                "authenticate",
                room_id=room,
                access_token=token,
                app_version="1",
                capabilities=[],
            )

        @asynccontextmanager
        async def joined(token):
            async with connect(url) as ws:
                await authenticate(ws, token)
                hello = await recv(ws, "joined")
                await recv(ws, "participants")
                yield ws, hello

        async def relay(ws, target, source, dest, kind, **payload):
            rid = await send(
                ws,
                kind,
                generation=source["generation"],
                target_user_id=dest["user_id"],
                target_generation=dest["generation"],
                **payload,
            )
            await recv(
                target, kind, request_id=rid, from_generation=source["generation"], **payload
            )

        async with joined(at) as (aw, aj), joined(bt) as (bw, bj):
            await recv(aw, "participants")
            if MODE == "shutdown":
                print("Now press Ctrl+C ONCE in the server terminal", flush=True)
                async with asyncio.timeout(12):
                    for ws in (aw, bw):
                        try:
                            while True:
                                msg = json.loads(await ws.recv())
                                print("WS", msg.get("type"), msg.get("code"), flush=True)
                        except ConnectionClosed:
                            print("OK WS ended", flush=True)
                return
            if MODE == "expiry":

                async def wait_expiry(ws, hello):
                    async with asyncio.timeout(80):
                        while True:
                            try:
                                raw = await asyncio.wait_for(ws.recv(), 2)
                            except TimeoutError:
                                await send(ws, "heartbeat", generation=hello["generation"])
                                continue
                            msg = json.loads(raw)
                            if msg.get("type") == "reauth_required":
                                check(
                                    msg["generation"] == hello["generation"],
                                    "reauth_required same generation",
                                )
                                return
                            check(msg.get("type") == "heartbeat_ack", "heartbeat before expiry")

                await asyncio.gather(wait_expiry(aw, aj), wait_expiry(bw, bj))
                await send(
                    aw,
                    "offer",
                    generation=aj["generation"],
                    target_user_id=bj["user_id"],
                    target_generation=bj["generation"],
                    sdp="v=0",
                )
                await recv(aw, "error", code="authorization_expired", fatal=False)
                refreshed = await http(
                    client,
                    "POST",
                    "/api/v1/auth/refresh",
                    200,
                    body={"refresh_token": a["refresh_token"]},
                )
                at = refreshed["access_token"]
                rid = await send(aw, "reauthenticate", generation=aj["generation"], access_token=at)
                await recv(aw, "reauthenticated", request_id=rid, generation=aj["generation"])

                # Keep both presence leases alive while B exhausts the auth grace period.
                async def keep(ws, hello):
                    while True:
                        await asyncio.sleep(2)
                        await send(ws, "heartbeat", generation=hello["generation"])

                tasks = [asyncio.create_task(keep(ws, j)) for ws, j in ((aw, aj), (bw, bj))]
                try:
                    async with asyncio.timeout(35):
                        while True:
                            msg = json.loads(await bw.recv())
                            if msg.get("type") == "heartbeat_ack":
                                continue
                            check(
                                msg.get("code") == "reauthentication_timeout" and msg.get("fatal"),
                                "grace expires",
                            )
                            break
                        await closed(bw)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                await http(client, "DELETE", path, 204, at)
                return
            if MODE == "revoke":
                await http(client, "POST", "/api/v1/auth/logout", 204, bt)
                msg = json.loads(await asyncio.wait_for(bw.recv(), 16))
                check(
                    msg.get("code") == "session_revoked" and msg.get("fatal"),
                    "revoked socket stopped",
                )
                await closed(bw)
                await recv(aw, "participants")
                await http(client, "DELETE", path, 204, at)
                return
            await relay(aw, bw, aj, bj, "offer", sdp="v=0")
            await relay(bw, aw, bj, aj, "answer", sdp="v=0")
            await relay(aw, bw, aj, bj, "ice_candidate", candidate=None)
            async with joined(bt) as (replacement, new):
                check(new["generation"] != bj["generation"], "new generation")
                await recv(aw, "participants")
                await recv(bw, "error", code="connection_replaced", fatal=True)
                await closed(bw)
                rid = await send(
                    aw,
                    "offer",
                    generation=aj["generation"],
                    target_user_id=bj["user_id"],
                    target_generation=bj["generation"],
                    sdp="v=0",
                )
                await recv(aw, "error", code="peer_replaced", fatal=False, request_id=rid)
                await relay(aw, replacement, aj, new, "offer", sdp="v=0")
                await http(client, "DELETE", path, 403, bt)
                await http(client, "DELETE", path, 204, at)
                for ws in (aw, replacement):
                    await recv(ws, "error", code="room_closed", fatal=True)
                    await closed(ws)
        state = await http(client, "GET", path, 200, at)
        check(state["state"] == "closed", "REST snapshot closed")
        async with connect(url) as ws:
            await authenticate(ws, at)
            await recv(ws, "error", code="room_closed", fatal=True)
            await closed(ws)


asyncio.run(main())
