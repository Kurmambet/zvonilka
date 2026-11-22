import argparse
import asyncio
import getpass
import json
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect


def expect(response, status):
    if response.status_code != status:
        raise RuntimeError(f"{response.request.method} {response.request.url.path}: "
                           f"HTTP {response.status_code}, expected {status}")


async def receive(ws, kind, request_id):
    async with asyncio.timeout(8):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "error":
                raise RuntimeError(f"WS error: {message.get('code')}")
            if message.get("type") == kind and message.get("request_id") == request_id:
                return message


async def main(args, password):
    base = urlsplit(args.base_url)
    ws_scheme = "wss" if base.scheme == "https" else "ws"
    # Optional lab override preserves HTTP Host and WebSocket URI authority.
    http_url = args.base_url if not args.connect_ip else urlunsplit(
        (base.scheme, args.connect_ip, "", "", "")
    )
    http_headers = {"Host": base.netloc} if args.connect_ip else {}
    ws_options = {"host": args.connect_ip} if args.connect_ip else {}
    async with httpx.AsyncClient(base_url=http_url, headers=http_headers,
                                 timeout=10, trust_env=False) as client:
        login = await client.post("/api/v1/auth/login", json={
            "username": args.username, "password": password,
        })
        expect(login, 200)
        token = login.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        room = await client.post("/api/v1/rooms", json={}, headers=headers)
        expect(room, 201)
        room_id = room.json()["room_id"]
        try:
            uri = f"{ws_scheme}://{base.netloc}/api/v1/rooms/{room_id}/ws"
            async with connect(uri, proxy=None, open_timeout=8, **ws_options) as ws:
                auth_id = str(uuid4())
                await ws.send(json.dumps({"version": 1, "type": "authenticate",
                                          "request_id": auth_id, "room_id": room_id,
                                          "access_token": token, "app_version": "1",
                                          "capabilities": []}))
                joined = await receive(ws, "joined", auth_id)
                generation = joined["generation"]
                print(f"ws.joined generation={generation} auth_request_id={auth_id}")
                await asyncio.sleep(joined["heartbeat_interval_ms"] / 1000)
                heartbeat_id = str(uuid4())
                await ws.send(json.dumps({"version": 1, "type": "heartbeat",
                                          "request_id": heartbeat_id,
                                          "generation": generation}))
                ack = await receive(ws, "heartbeat_ack", heartbeat_id)
                if ack.get("generation") != generation:
                    raise RuntimeError("heartbeat_ack generation mismatch")
                print("OK: heartbeat_ack")
            print("OK: WebSocket closed")
        finally:
            closed = await client.delete(f"/api/v1/rooms/{room_id}", headers=headers)
            if closed.status_code != 204:
                print(f"WARNING: room cleanup HTTP {closed.status_code}; room={room_id}")
            else:
                print("OK: room closed")
        expect(closed, 204)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://api.zvonilka.website")
    parser.add_argument("--username", required=True)
    parser.add_argument("--connect-ip", help="Only for the isolated HTTP libvirt lab")
    args = parser.parse_args()
    base = urlsplit(args.base_url)
    if (base.scheme not in {"http", "https"} or not base.hostname or base.username
            or base.password or base.path not in {"", "/"} or base.query or base.fragment):
        parser.error("--base-url must be an HTTP(S) origin")
    if base.scheme == "http" and (base.netloc != "zvonilka-lab.test"
                                  or args.connect_ip != "192.168.100.14"):
        parser.error("plain HTTP is allowed only for the configured libvirt lab")
    if base.scheme == "https" and args.connect_ip:
        parser.error("use public DNS for HTTPS")
    asyncio.run(main(args, getpass.getpass("Smoke account password: ")))
