# Фактический статус

## Написано
Redis Lua-транзакция и Python adapter: комнаты/квоты/активация, допуск,
одноразовые приглашения, hash-only хранение, TTL, отзыв, lost-response retry
погашения, presence leases, generation fencing, close и sweep.
Написаны отдельные unit- и real-Redis integration tests.

## Выполнено в этой среде
Python unittest adapter/validation и compileall; полный вывод в TEST-RESULTS.txt.
Unit-тесты используют RecordingRedis и НЕ исполняют Lua.

## Заблокировано средой / не проверено
Нет redis-server, Docker, redis-py и Lua runtime. Lua не исполнялся и даже не
проверен парсером Lua. Интеграционные сценарии автоматически skipped без URL;
это НЕ успешная проверка Redis. Нужно запустить их с настоящим Redis.
Нет доступа к package registry из Python execution: uv sync/build/lock и
проверка совместимости выбранного диапазона redis-py не выполнялись.
pyproject.toml задаёт диапазоны, не проверенный lock. Frozen CI пока невозможен.

## Не завершено
Это не полный backend: нет FastAPI, SQLAlchemy/PostgreSQL/Alembic, пользователей,
JWT/refresh, rate limits, REST/WSS, lifespan sweeper, resource guard и telemetry.
Нет Electron/Vue/WebRTC/coturn, helper/DSP/AEC, media, deployment/monitoring,
CI/release installers, замеров VPS или сетевой/аппаратной приёмки.

## Следующий gate
1. Выполнить интеграционные тесты на настоящем Redis и исправить ошибки.
2. Замерить Lua в максимальном разрешённом состоянии.
3. Получить проверенный uv.lock и wheel с вложенным transaction.lua.
4. Добавить API/auth/PG, затем WS с generation fencing и реальный desktop.

Production-ready не заявляется. Изменения не публиковались в GitHub и не
развёртывались на VPS. Архив — самостоятельная частичная исходная поставка.
