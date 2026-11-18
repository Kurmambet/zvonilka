```bash
export ZVONILKA_TEST_DATABASE_URL='postgresql+asyncpg://zvonilka:пароль@127.0.0.1:5432/zvonilka_test'

export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'

PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli reset-password alice
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli cleanup


PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.server

# backend/
uv run python -m zvonilka.testing.runner --start tests --pattern test_rooms_api_integration.py --require-no-skips
```

# Redis

```bash
sudo apt update
sudo apt install redis-server

sudo systemctl start redis-server
sudo systemctl status redis-server
redis-cli ping


ss -ltn '( sport = :6379 )'

redis-cli CONFIG GET bind
redis-cli CONFIG GET protected-mode
```

Для локального экземпляра ожидаем loopback-адреса, например 127.0.0.1/::1, и включённый protected mode.

запуск для финального теста сигналинга:
/backend

```bash

stamp=$(date +%s)
ZVONILKA_ROOM_REGISTRY_KEY="zvonilka:manual:$stamp" \
ZVONILKA_RATE_LIMIT_PREFIX="zvonilka:manual:$stamp" \
ZVONILKA_ACCESS_TTL_SECONDS=60 \
uv run uvicorn zvonilka.api:create_app --factory \
  --host 127.0.0.1 --port 8000 --workers 1 --timeout-graceful-shutdown 5
```

из другого терминала /backend

```bash
curl -fsS http://127.0.0.1:8000/health/ready
uv run python signaling-smoke-4-6-2.py rooms
uv run python signaling-smoke-4-6-2.py revoke
uv run python signaling-smoke-4-6-2.py expiry   - 2 минуты гдето
uv run python signaling-smoke-4-6-2.py shutdown - надо завершить процесс сервера в нужный момент
```
