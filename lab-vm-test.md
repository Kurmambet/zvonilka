```bash
ansible-playbook zvonilka-lab.yml --limit lab-vm -e zvonilka_revision=HEAD


curl --noproxy '*' --max-time 5 -sS -D - \
  --resolve zvonilka-lab.test:80:192.168.100.14 \
  -H 'Content-Type: application/json' \
  --data '{}' \
  http://zvonilka-lab.test/api/v1/rooms
```

```bash
uv run --with httpx --with 'websockets>=15,<18' python - <<'PY'
import asyncio
import json
import secrets
import subprocess
from uuid import uuid4

import httpx
from websockets.asyncio.client import connect

VM = "192.168.100.14"
HOST = "zvonilka-lab.test"


def check_status(response, expected):
    if response.status_code != expected:
        raise RuntimeError(
            f"{response.request.method} {response.request.url.path}: "
            f"HTTP {response.status_code}, ожидался {expected}"
        )


async def receive(ws, kind, request_id):
    async with asyncio.timeout(8):
        while True:
            message = json.loads(await ws.recv())
            if message.get("type") == "error":
                raise RuntimeError(f"WS error: {message.get('code')}")
            if message.get("type") == kind and message.get("request_id") == request_id:
                return message


async def main():
    username = "lab_" + secrets.token_hex(5)
    credentials = {"username": username, "password": secrets.token_urlsafe(24)}

    subprocess.run(
        [
            "ssh", "-T", f"ubuntu@{VM}",
            "curl -fsS --max-time 10 -o /dev/null "
            "-H 'Content-Type: application/json' --data-binary @- "
            "http://127.0.0.1:8002/api/v1/auth/register",
        ],
        input=json.dumps({**credentials, "display_name": "Lab smoke test"}),
        text=True,
        check=True,
    )
    print(f"OK: аккаунт {username} создан через loopback")

    async with httpx.AsyncClient(
        base_url=f"http://{VM}",
        headers={"Host": HOST},
        trust_env=False,
        timeout=8,
    ) as client:
        login = await client.post("/api/v1/auth/login", json=credentials)
        check_status(login, 200)
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
        print("OK: логин через Nginx")

        room = await client.post("/api/v1/rooms", json={}, headers=headers)
        check_status(room, 201)
        room_id = room.json()["room_id"]
        print(f"OK: комната {room_id} создана через Nginx")

        try:
            async with connect(
                f"ws://{HOST}/api/v1/rooms/{room_id}/ws",
                host=VM,
                port=80,
                proxy=None,
                open_timeout=8,
            ) as ws:
                auth_id = str(uuid4())
                await ws.send(json.dumps({
                    "version": 1,
                    "type": "authenticate",
                    "request_id": auth_id,
                    "room_id": room_id,
                    "access_token": login.json()["access_token"],
                    "app_version": "1",
                    "capabilities": [],
                }))
                joined = await receive(ws, "joined", auth_id)
                generation = joined["generation"]
                print(f"OK: ws.joined · generation={generation} · auth_request_id={auth_id}")


                heartbeat_interval_ms = joined["heartbeat_interval_ms"]
                print(f"Ждём {heartbeat_interval_ms} мс до первого heartbeat")
                await asyncio.sleep(heartbeat_interval_ms / 1000)

                heartbeat_id = str(uuid4())
                await ws.send(json.dumps({
                    "version": 1,
                    "type": "heartbeat",
                    "request_id": heartbeat_id,
                    "generation": generation,
                }))
                ack = await receive(ws, "heartbeat_ack", heartbeat_id)
                if ack.get("generation") != generation:
                    raise RuntimeError("heartbeat_ack вернул другой generation")
                print("OK: heartbeat_ack через Nginx")

            print("OK: WebSocket штатно закрыт")
        finally:
            closed = await client.delete(f"/api/v1/rooms/{room_id}", headers=headers)
            if closed.status_code == 204:
                print("OK: тестовая комната закрыта")
            else:
                print(f"Внимание: закрытие тестовой комнаты вернуло HTTP {closed.status_code}")


asyncio.run(main())
PY

```

```bash
GENERATION='ВСТАВЬ_UUID_ИЗ_СТРОКИ_ws.joined'

ssh ubuntu@192.168.100.14 \
  'sudo docker logs --since 10m zvonilka-staging-api-1 2>&1' \
  | grep -F -- "$GENERATION"
```

# test reboot

```bash
ssh ubuntu@192.168.100.14 'sudo systemctl reboot'

ssh ubuntu@192.168.100.14 \
  'uptime -s; systemctl is-active docker nginx; sudo docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"'

curl --noproxy '*' --max-time 5 -fsS \
  --resolve zvonilka-lab.test:80:192.168.100.14 \
  http://zvonilka-lab.test/health/ready


for path in /api/v1/auth/register /api/v1/auth/register/; do
  curl --noproxy '*' --max-time 5 -sS -o /dev/null \
    -w "$path → %{http_code}\n" \
    --resolve zvonilka-lab.test:80:192.168.100.14 \
    -X POST "http://zvonilka-lab.test$path"
done

ssh ubuntu@192.168.100.14 \
  'sudo docker ps -a --filter name=zvonilka-staging; sudo docker logs --tail 50 zvonilka-staging-api-1'

```
