```bash
export ZVONILKA_TEST_DATABASE_URL='postgresql+asyncpg://zvonilka:пароль@127.0.0.1:5432/zvonilka_test'

export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'

PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli reset-password alice
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli cleanup

# запуск сервера
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.server

# применение паичей:
sha256sum signaling-rest-close-4-6-1.patch
git apply --check signaling-rest-close-4-6-1.patch && git apply signaling-rest-close-4-6-1.patch

# запуск тестов:
# backend/

uv run ruff format src/zvonilka/rooms/router.py src/zvonilka/signaling/participants.py \
  tests/test_signaling_participants_unit.py tests/test_signaling_relay_integration.py
uv run ruff check src/zvonilka/rooms/router.py src/zvonilka/signaling tests/test_signaling_*.py

uv run python -m zvonilka.testing.runner --start tests --pattern test_rooms_api_integration.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_participants_unit.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_relay_integration.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_rooms_api_integration.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
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

# полный прогон smoke в докере.

```bash
if [ ! -e .env.compose.local ]; then
  (
    umask 077
    python3 -c 'import secrets; print("ZVONILKA_DEV_JWT_SECRET=" + secrets.token_urlsafe(48))' \
      > .env.compose.local
  )
fi

git check-ignore .env.compose.local
docker compose version

dc() { docker compose --env-file .env.compose.local -f compose.dev.yaml "$@"; }

dc config --quiet
dc up --build -d
dc ps -a
curl -fsS http://127.0.0.1:8001/health/ready
```

Дальше две пары прогонов, а не все четыре на одном префиксе: каждый режим регистрирует по два аккаунта, и иначе снова получим 429 на шестой регистрации.

```bash
# Из корня репозитория; первая пара на обычном TTL.
export ZVONILKA_SMOKE_BASE_URL=http://127.0.0.1:8001
stamp=$(date +%s)

export ZVONILKA_DEV_RATE_LIMIT_PREFIX="zvonilka:compose:smoke-a:$stamp"
export ZVONILKA_DEV_ROOM_REGISTRY_KEY="zvonilka:compose:rooms-a:$stamp"
export ZVONILKA_DEV_ACCESS_TTL_SECONDS=600
dc up -d --no-deps --force-recreate api
curl -fsS "$ZVONILKA_SMOKE_BASE_URL/health/ready"

(
  cd zvonilka/backend
  uv run python signaling-smoke-4-6-2.py rooms &&
    uv run python signaling-smoke-4-6-2.py revoke
)
# После успешной первой пары:

# Вторая пара: свежий префикс и короткий TTL для expiry.
export ZVONILKA_DEV_RATE_LIMIT_PREFIX="zvonilka:compose:smoke-b:$stamp"
export ZVONILKA_DEV_ROOM_REGISTRY_KEY="zvonilka:compose:rooms-b:$stamp"
export ZVONILKA_DEV_ACCESS_TTL_SECONDS=60
dc up -d --no-deps --force-recreate api
curl -fsS "$ZVONILKA_SMOKE_BASE_URL/health/ready"

(
  cd zvonilka/backend
  uv run python signaling-smoke-4-6-2.py expiry &&
    uv run python signaling-smoke-4-6-2.py shutdown
)



# в момент, когда надо будет остановить докер, из другого терминала:
docker compose --env-file .env.compose.local -f compose.dev.yaml stop api
docker compose --env-file .env.compose.local -f compose.dev.yaml logs --tail=50 api

unset ZVONILKA_DEV_RATE_LIMIT_PREFIX ZVONILKA_DEV_ROOM_REGISTRY_KEY \
      ZVONILKA_DEV_ACCESS_TTL_SECONDS ZVONILKA_SMOKE_BASE_URL

```

# работа с compose

```bash
dc() { docker compose --env-file .env.compose.local -f compose.dev.yaml "$@"; }
dc config --quiet

dc up -d
dc up --build -d
dc up -d --no-deps --force-recreate api

dc ps
dc ps -a
dc logs --tail=80 migrate api


dc down
dc down -v
```

Создание и работа с electron /desktop/:

```bash
cd zvonilka
npm create @quick-start/electron@latest
# Имя проекта: desktop
# Framework: vue
# TypeScript: Yes
# Updater: No
cd desktop
npm install
npm run dev
```

```bash
docker compose --env-file .env.compose.local -f compose.dev.yaml exec postgres psql -U zvonilka -d zvonilka

  SELECT username, display_name FROM auth_users WHERE username = 'test2user';

docker compose --env-file .env.compose.local -f compose.dev.yaml exec postgres psql -U zvonilka -d zvonilka -c "SELECT username, display_name FROM auth_users LIMIT 10;"




docker compose --env-file .env.compose.local -f compose.dev.yaml exec postgres psql -U zvonilka -d zvonilka -c "\dt"

docker compose --env-file .env.compose.local -f compose.dev.yaml stop api
```

```bash
ZVONILKA_DEV_ACCESS_TTL_SECONDS=60 \
  docker compose --env-file .env.compose.local -f compose.dev.yaml \
  up -d --no-deps --force-recreate api


docker compose --env-file .env.compose.local -f compose.dev.yaml \
  exec -T api printenv ZVONILKA_ACCESS_TTL_SECONDS

curl -fsS http://127.0.0.1:8001/health/ready

# пересоздать
unset ZVONILKA_DEV_ACCESS_TTL_SECONDS
docker compose --env-file .env.compose.local -f compose.dev.yaml \
  up -d --no-deps --force-recreate api
```

```bash
# пересборка api
docker compose --env-file .env.compose.local -f compose.dev.yaml \
  up -d --build --no-deps --force-recreate api

curl -fsS http://127.0.0.1:8001/health/ready

npm run typecheck && npm run lint
npm run dev
```
