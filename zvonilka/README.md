# Zvonilka — этап 2: Redis repository

Частичная исходная поставка. Это ещё не запускаемый API и не звонилка.
Здесь реализован новый Redis-адаптер комнат и приглашений, Lua-транзакция,
unit-тесты Python-границы и интеграционные тесты для настоящего Redis.
Архив самостоятельный: файлы архива этапа 1 не нужны.

## Быстрые unit-тесты без внешних пакетов

Python 3.12+:

```bash
bash scripts/test-unit.sh
```

Windows PowerShell:

```powershell
cd backend
$env:PYTHONPATH = "$PWD/src"
python -m unittest discover -s tests -p test_repository.py -v
```

## Реальные интеграционные тесты

Нужен отдельный тестовый Redis 7.x в standalone-режиме. Не production.
Установка зависимостей ниже не frozen: проверенный uv.lock пока отсутствует.

```bash
cd backend
uv sync
cd ..
export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
# Вариант без активации venv:
cd backend
ZVONILKA_TEST_REDIS_URL="$ZVONILKA_TEST_REDIS_URL" PYTHONPATH=src \
  uv run python -m unittest discover -s tests -p test_redis_integration.py -v
```

Или активируйте backend/.venv и запустите `bash scripts/test-redis.sh` из корня.
Если Redis требует пароль, включите его в URL, не публикуя URL в отчётах.
Каждый тест использует собственный случайный ключ; FLUSHDB/FLUSHALL не вызываются.
Переменная с URL отсутствует — integration tests явно skipped. Если URL задан,
ошибка подключения/отсутствующий пакет считается ошибкой, не пропуском.

Тесты не следует запускать против Redis с eviction: repository требует
`maxmemory-policy noeviction`. Потеря ключа означает потерю временных комнат.

## Интеграция в будущий API

```python
from redis.asyncio import Redis
from zvonilka.rooms.repository import RoomRepository

redis = Redis.from_url(redis_url, socket_timeout=5, socket_connect_timeout=5)
rooms = RoomRepository(redis)
# actor_id только из проверенной сервером сессии!
room = await rooms.create(actor_id)
invite = await rooms.issue_invite(actor_id, room["room_id"])
```

Методы — внутренний интерфейс, не публичные endpoints. Авторизации,
rate limits, HTTP/WSS и валидации протокола здесь ещё нет.

Подробнее: docs/REDIS-DESIGN.md, docs/STATUS.md и фактические результаты тестов.

Да, поставку 4.3 закрываем: Ruff чистый, signaling — **129/129**, без skips. Ошибка coroutine устранена.

Несколько воркеров сделать сможем в будущем. Сейчас ограничение не в Gunicorn, а в нашем локальном реестре WS-соединений.

## Почему сейчас один worker

У каждого процесса своя память. Например:

```text
Worker 1: соединение Алисы, её Outbox, локальный ParticipantsHub
Worker 2: соединение Боба, его Outbox, другой ParticipantsHub
```

Алиса и Боб могут попасть в разные процессы. Тогда каждый процесс видит только своего участника: общий Redis хранит admission и presence, но сам по себе не доставляет сообщения в очереди другого процесса. Это обычное ограничение in-memory WS-менеджеров при нескольких workers или репликах.

Для нашей реализации это означает:

- неполные снимки `participants`;
- невозможность напрямую переслать сообщение адресату другого worker;
- невозможность одним локальным `Connection.stop()` остановить старое соединение в другом процессе.

Позже понадобится межпроцессная доставка, например через Redis Pub/Sub, плюс согласование поколений, порядка снимков и восстановления после пропущенных событий. Просто добавить `-w 4` недостаточно. Межпроцессный брокер — стандартный способ связать локальные WS-реестры.

Поэтому сейчас фиксируем: **один процесс API и одна его реплика**. Четыре контейнера с одним worker в каждом создали бы ту же проблему.
