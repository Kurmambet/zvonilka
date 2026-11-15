# Этап 5 — REST API комнат и приглашений

## Применение

Overlay распаковывается во ВНУТРЕННИЙ zvonilka/, содержащий backend/, scripts/,
docs/. Перед применением сохраните текущие изменения в Git.

ЗАМЕНЯЮТСЯ только backend/src/zvonilka/api.py и config.py, на основе присланного
pr_structure.md. Остальные файлы новые. pyproject.toml, uv.lock, .env,
repository.py, transaction.lua, миграции и существующие тесты не заменяются.
Новых зависимостей и миграций PostgreSQL нет. Имеющиеся настройки и данные auth
сохраняются. В срезе нет содержимого transaction.lua; используется проверенная
пользователем версия с сортировкой members. Его наличие в установленном пакете
обязательно. Этот архив не является самостоятельной полной поставкой проекта.

backend/rooms.env.example содержит только новые параметры. Добавлять их в .env
необязательно: указанные значения — defaults. Не заменяйте им существующий .env.

## Проверки

Из внутреннего zvonilka/:

```bash
bash scripts/check-rooms-unit.sh
# 17 service/codec/error mapping + 5 schemas + 3 config = 25 тестов.

# Существующие тестовые URL PostgreSQL/Redis должны быть выставлены.
bash scripts/check-rooms-integration.sh
# 21 API-тест с настоящими PostgreSQL и Redis.

bash scripts/check-auth-unit.sh
bash scripts/check-auth-integration.sh
bash scripts/check-unit-compact.sh
bash scripts/check-redis-compact.sh
```

Из backend/:

```bash
uv run ruff check .
uv run ruff format --check src tests
```

Из внутреннего zvonilka/ проверьте также scripts/:

```bash
uv run ruff check backend/src backend/tests scripts
```

Ruff в среде автора недоступен и отсутствует в offline cache: он не запускался.
Если formatter потребует изменения, выполните `uv run ruff format src tests`
из backend/ и затем повторите lint/tests. Это не замена проверке логики.

## Контракт REST

Все операции требуют Authorization: Bearer <access token>.

| Метод | Путь | Результат |
|---|---|---|
| POST | /api/v1/rooms | 201, созданная неактивированная комната; JSON-тело {} обязательно |
| GET | /api/v1/rooms/{room_id} | 200, снимок для допущенного пользователя |
| POST | /api/v1/rooms/{room_id}/invites | 201, новое приглашение; только владелец |
| DELETE | /api/v1/rooms/{room_id}/invites/{invite_id} | 204, отзыв непогашенного приглашения; только владелец |
| POST | /api/v1/invites/redeem | 200, допуск и текущий снимок комнаты |
| DELETE | /api/v1/rooms/{room_id} | 204, закрытие владельцем |

В POST /rooms нельзя передавать owner_id/user_id или другие поля. Владелец
берётся из Principal текущей проверенной сессии. Остальные операции также
используют только текущий Principal, а не идентификатор пользователя из тела.

Комната возвращает room_id, owner_id, state, revision, members (user_id/online),
activated_at, deadline, empty_at. Временные поля — UTC datetime либо null.
Порядок members определяется канонической сортировкой существующей Lua-версии,
а не временем входа. Список — допущенные аккаунты, не только онлайн-участники.
Имена профилей пока не подмешиваются; это технический контракт для следующего
клиентского этапа. Не делать N+1 SQL-запрос по каждому участнику.

## Приглашения

Ответ issue:

```json
{
  "invite_id": "<64 lowercase hex characters>",
  "room_id": "<room UUID>",
  "invitation": "zv1.<canonical room UUID>.<43-character token>",
  "expires_at": "<UTC datetime>"
}
```

Это строка для копирования/вставки, НЕ действующий веб-сайт, deep link или QR.
Парсер ограничен версией zv1 и канонической формой. URL/deep links пока не
поддерживаются. Владелец получает секрет только при выдаче; в Redis только SHA-256.
invite_id — хеш/handle для отзыва, а не секрет погашения. Сохраните его в клиенте,
если нужно отозвать конкретную ссылку. Полный секрет нельзя писать в логи.

Погашение после явного подтверждения в будущем клиенте:

```json
{"invitation": "zv1.<room UUID>.<token>"}
```

Строка передаётся только в JSON POST /api/v1/invites/redeem, не в URL/query.
GET /api/v1/invites/redeem возвращает 405 и ничего не погашает. Тело запроса
не содержит account ID, одно приглашение нельзя одновременно погасить двумя
аккаунтами. Тот же аккаунт может повторить успешный redeem после потери ответа,
пока комната не закрыта: допуск не теряется, новое место не расходуется.
Возвращается текущий снимок, а не навечно сохранённый старый HTTP-ответ.

Повторное подключение в будущем signaling использует допуск, не invite.
Перевыпуск сейчас состоит из revoke старого + issue нового: это две операции,
не одна атомарная замена. Выдача и создание комнаты не имеют полной HTTP
идемпотентности: после timeout не повторять их автоматически бесконечно.
Потерянный ответ issue может оставить недоступный секрет до TTL; листинга
приглашений/идемпотентного восстановления issue в этом этапе нет.

## Admission и сроки

POST /rooms создаёт pending-комнату, не занимает активный слот.
Redeem сохраняет допуск, но не делает online/active. Владельцу также не ставится
presence автоматически. Нельзя занимать активный слот просмотром/копированием.

Активация, присутствие, heartbeat и leave не опубликованы как HTTP endpoints.
Их свяжет WebSocket в следующем этапе. Поэтому через один Swagger пока нельзя
показать реальный online/active-звонок. Это не заглушка: REST решает подготовку
комнаты и допуск, а не медиаподключение.

Сроки и реальная атомарность — в существующем Redis Lua. Все проверки квот
остаются там; Python/router не выполняют небезопасную связку count -> create.
RoomRepository выполняет уборку перед операциями. Отдельный lifespan sweeper
пока не добавлен: физическая уборка происходит при следующей операции, но
логически просроченная комната уже не может использоваться. Background sweep
связать с signaling и его presence lifecycle. Не считать TTL полей записью
Redis EXPIRE всего aggregate: это существующая модель с bounded JSON.

Все лимиты из Settings преобразуются в Limits через settings.room_limits().
Поддерживается только admission_mode=static. guarded отвергается конфигурацией,
пока нет настоящего измерителя VPS, окон и hysteresis. Ограничение registry
128 записей и 32 invitation history на комнату сохранено. Закрытые записи
удерживаются closed_retention, поэтому возможен явный отказ registry_full
при частом создании/закрытии. Не увеличивать safety bounds без benchmark.

## Ошибки и rate limits

Общий формат прежний: error.code + request_id; Cache-Control: no-store.

- 401 invalid_access — нет действующей сессии (включая отозванную).
- 403 not_admitted/not_owner — нет допуска/прав владельца.
- 404 room_not_found/invite_not_found — нет записи.
- 409 pending_limit/room_limit/owner_room_limit/participant_limit и другие конфликты.
- 410 room_closed/invite_revoked/invite_expired — ресурс больше недействителен.
- 422 invalid_request — неверный UUID, формат приглашения или лишние поля.
- 429 rate_limited — ограничение запросов.
- 503 redis_unavailable — Redis-команда не удалась, в том числе rate limiter.
- 503 registry_full/schema_mismatch — ресурсный/версионный отказ room aggregate.
- Неизвестные RoomError не выдаются пользователю как произвольный текст: 500 internal_error.

Изменён старый неточный код rate_limit_unavailable: не всякий RedisError теперь
связан только с rate limits. Истинная причина и секреты не возвращаются клиенту.

Ограничения по текущему authenticated account: создание 12/час, чтение 120/мин,
issue/revoke/close суммарно 30/5мин, redeem 30/5мин. Неуспешный бизнес-запрос после
валидного разбора тела также расходует квоту. Неверный формат тела отклоняется
раньше handler и не расходует эту пользовательскую квоту. Это не замена лимиту
трафика reverse proxy. Rate limiter использует Redis и существующий namespace
rate_limit_prefix, room aggregate — отдельный room_registry_key.

## Локальный smoke test через Swagger

1. Войти владельцем, POST /rooms с {}, сохранить room_id.
2. POST /rooms/{id}/invites, сохранить invitation и invite_id.
3. Войти другим аккаунтом; GET комнаты должен дать 403 до допуска.
4. POST /invites/redeem с invitation: 200, два разных user_id, state=created.
5. Повторить тем же аккаунтом: 200, нет дополнительного участника.
6. Допущенным гостем попытаться закрыть комнату: 403.
7. Владельцем DELETE /rooms/{id}: 204; новое погашение запрещено.

Не отправлять в чат настоящие access/refresh/invite tokens.

## Границы проверки

Здесь реально выполнены только перечисленные в STEP-5-TEST-RESULTS.md проверки.
Unit boundary doubles не доказывают Lua/HTTP/БД. 21 integration tests требуют
настоящих PostgreSQL/Redis и исполняемого FastAPI-окружения на вашей машине.
Они создают отдельную случайную PostgreSQL-схему, отдельный Redis aggregate key,
отдельный rate prefix. Не используют FLUSHDB, не удаляют рабочий aggregate.
Argon2 в этих тестах настоящий, но с пониженной стоимостью; production-параметры
покрывает существующий test_auth_passwords.py.

НЕТ WebSocket, TURN credentials, настоящих медиаподключений, desktop,
платформенного аудио, resource guard или замеров VPS. Это следующий REST-этап,
а не заявление о завершении Zvonilka.
