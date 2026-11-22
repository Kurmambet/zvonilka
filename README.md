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
docker compose --env-file .env.compose.local -f compose.dev.yaml logs -f --tail=50 api



unset ZVONILKA_DEV_RATE_LIMIT_PREFIX ZVONILKA_DEV_ROOM_REGISTRY_KEY \
      ZVONILKA_DEV_ACCESS_TTL_SECONDS ZVONILKA_SMOKE_BASE_URL

```

# работа с compose

```bash
docker compose --env-file .env.compose.local -f compose.dev.yaml up --build -d


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

Обновленное применение патчей:

```bash
PATCH="$HOME/Downloads/zvonilka-three-peer-audio.patch"  # укажи фактический путь
git apply --check --unidiff-zero "$PATCH"

git apply --unidiff-zero "$PATCH"
git diff --check
cd zvonilka/desktop
npm run typecheck && npm run lint
npm run dev
```

Работа на Шindows:

```bash
node -e "console.log('ZVONILKA_DEV_JWT_SECRET=' + require('node:crypto').randomBytes(48).toString('hex'))" Set-Content -Encoding ascii .env.compose.local

docker compose version
docker compose --env-file .env.compose.local -f compose.dev.yaml up -d
curl.exe -fsS http://127.0.0.1:8001/health/ready





cd zvonilka\desktop
npm.cmd ci
npm.cmd run typecheck
npm.cmd run lint
npm.cmd run dev

test1
oioioi_Test-1

test2
oioioi_Test-2

test3
oioioi_Test-3

```

найти отчет по поколению

2026-11-22T04:26:22.466Z · ws.open · auth=824ec44a-eed4-487f-a2c8-7d1d0b19da74
2026-11-22T04:26:22.488Z · ws.joined · auth=824ec44a-eed4-487f-a2c8-7d1d0b19da74 · generation=aa7ae704-a35e-4fd2-8579-1f602228b6fb
2026-11-22T04:26:37.507Z · ws.heartbeat-ack · auth=824ec44a-eed4-487f-a2c8-7d1d0b19da74 · generation=aa7ae704-a35e-4fd2-8579-1f602228b6fb

```bash
docker compose --env-file .env.compose.local -f compose.dev.yaml logs --no-color api \
 | grep -F 'aa7ae704-a35e-4fd2-8579-1f602228b6fb'



docker compose --env-file .env.compose.local -f compose.dev.yaml exec -T api \
  python -c 'import os; from zvonilka.config import Settings; print("env:", os.getenv("ZVONILKA_ENVIRONMENT")); print("settings:", Settings().environment)'

# env: None
# settings: dev

docker compose --env-file .env.compose.local -f compose.dev.yaml exec -T -e ZVONILKA_ENVIRONMENT=staging api \
  python -c 'import os; from zvonilka.config import Settings; print("env:", os.getenv("ZVONILKA_ENVIRONMENT")); print("settings:", Settings().environment)'

```

docker compose --env-file .env.compose.local -f compose.dev.yaml exec -T api \
 python -c 'import os; from zvonilka.config import Settings; print("env:", os.getenv("ZVONILKA_ENVIRONMENT")); print("settings:", Settings().environment)'

docker compose --env-file .env.compose.staging -f compose.staging.yaml -T api \
 python -c 'import os; from zvonilka.config import Settings; print("env:", os.getenv("ZVONILKA_ENVIRONMENT")); print("settings:", Settings().environment)'
