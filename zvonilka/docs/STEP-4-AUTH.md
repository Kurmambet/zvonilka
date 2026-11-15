# Этап 4: аккаунты, PostgreSQL и отзываемые сессии

## Встраивание

Распаковать overlay в каталог с backend/, scripts/ и docs/. Зависимости и lock
не заменяются. Нужны предыдущие этапы 2 и 3. Этот архив не заменяет исправленный
transaction.lua и не затрагивает комнаты. Python 3.12+.

## 1. Зависимости (из backend/)

```bash
env -u VIRTUAL_ENV uv add \
  'fastapi>=0.115,<1' 'uvicorn>=0.30,<1' \
  'sqlalchemy[asyncio]>=2.0,<2.1' 'asyncpg>=0.29,<1' \
  'alembic>=1.13,<2' 'pydantic-settings>=2.7,<3' \
  'PyJWT>=2.10,<3' 'argon2-cffi>=23.1,<26' 'anyio>=4.8,<5'
env -u VIRTUAL_ENV uv add --dev 'httpx>=0.27,<1'
```

redis-py уже является зависимостью этапа 2. Это диапазоны API, а не заявление
о проверенной матрице версий. uv.lock формируется у пользователя. В этой среде
SQLAlchemy/FastAPI/PyJWT/asyncpg/Alembic/pydantic-settings отсутствуют; разрешение
зависимостей, полный lint/typecheck и интеграционный запуск не выполнены.

## 2. Базы

Нужны PostgreSQL и уже работающий Redis. PostgreSQL роль приложения не должна
быть суперпользователем. Пример команд внутри административного psql:

```sql
CREATE ROLE zvonilka LOGIN PASSWORD 'replace-with-a-strong-database-password';
CREATE DATABASE zvonilka OWNER zvonilka;
CREATE DATABASE zvonilka_test OWNER zvonilka;
```

Тестовую БД не использовать как production. Роль в тестовой БД должна иметь
право CREATE SCHEMA: tests создают/удаляют только собственные случайные схемы.
Если роль уже существует, не выполнять CREATE ROLE повторно. URL-encode символы
пароля в DSN. Production-провижининг, секреты и backup будут отдельным этапом.

## 3. Конфигурация (из backend/)

```bash
cp auth.env.example .env
chmod 600 .env
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Заменить DATABASE_URL, REDIS_URL и JWT_SECRET в .env. Placeholder JWT отклоняется.
Не добавлять .env в Git. Settings читает .env из текущего рабочего каталога;
в командах ниже это backend/. Экспортированные env имеют приоритет над .env.
Миграционный wrapper сам переходит в backend/.

## 4. Миграция (из backend/)

```bash
env -u VIRTUAL_ENV uv run python ../scripts/migrate-auth.py
```

Ожидается MIGRATION_OK head. Повторный upgrade безопасен. Приложение не вызывает
create_all при запуске. Начальная миграция создаёт auth_users/auth_sessions/
auth_refresh_tokens и alembic_version. Связи имеют ON DELETE CASCADE.
Offline SQL миграции пока не поддержаны. Downgrade удаляет аккаунты и сессии:
не выполнять без backup. Автоматического отката БД нет.

## 5. Локальный сервер (из backend/)

```bash
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.server
```

Один процесс, 127.0.0.1:8000, без доверия X-Forwarded-For, без access log.
Swagger: http://127.0.0.1:8000/docs. Ready: /health/ready; live: /health/live.
PostgreSQL и Redis обязательны при старте. Миграция должна быть применена раньше:
startup ping пока проверяет доступность БД, а не наличие ревизии Alembic.
Это локальный dev-запуск, не публичный production-сервер. HTTPS, trusted proxy,
CORS для desktop и инфраструктура ещё не настроены. Не открывать порт 8000 наружу.
Behind proxy все клиенты пока разделяют IP-квоту proxy; не включать доверие
произвольному X-Forwarded-For ради обхода этой проблемы.

## REST

| Метод | Endpoint | Результат |
|---|---|---|
| POST | /api/v1/auth/register | 201, профиль без токенов |
| POST | /api/v1/auth/login | access + refresh + session_id |
| POST | /api/v1/auth/refresh | новая пара, прежний refresh consumed |
| GET | /api/v1/users/me | профиль по Bearer access |
| GET | /api/v1/auth/sessions | только свои активные сессии |
| DELETE | /api/v1/auth/sessions/{UUID} | отзыв своей сессии |
| POST | /api/v1/auth/logout | отзыв текущей сессии |
| POST | /api/v1/auth/password | смена пароля, отзыв ВСЕХ сессий |

Регистрация: username/display_name/password. Логин: username/password.
Refresh: refresh_token. Смена пароля: current_password/new_password.
Все credentials передаются в JSON body, не query string.
Ответы содержат Cache-Control: no-store и сгенерированный X-Request-ID.
Ошибки: {"error":{"code":"invalid_credentials","request_id":"..."}}.
Валидация не возвращает исходные input или стандартный exc.errors(), где мог
бы оказаться пароль. Неизвестная ошибка даёт internal_error, не str(exception).
Аргон2 ошибки повреждённого хеша не маскируются под неверный пароль.

## Правила аккаунта

Username: trim ASCII whitespace, lowercase, 3..32 символа, начинается a-z,
остальные a-z/0-9/underscore. Входной не-ASCII username отклоняется до lowercase,
в том числе Unicode Kelvin sign. Уникальность дополнительно гарантирует БД.
Display name: trim, 1..64, без C0/C1 control characters и Unicode surrogates. Пароль не нормализуется
и не trim: 12..128 Unicode символов, до 512 UTF-8 байт.

Argon2id: memory 65536 KiB, time_cost=3, parallelism=1, максимум два одновременных
хеширования/проверки на процесс. Работа вынесена через anyio.to_thread, не блокирует
сам event loop. Dummy verify применяется для несуществующего username, но это
не доказательство абсолютного отсутствия всех timing side channels.
Rehash выполняется после успешного входа при изменении параметров.

## JWT и refresh

Access: HS256 только, 10 минут. Проверяются подпись, exp/iat/nbf, issuer,
audience, обязательные claims, тип access и UUID sub/sid/jti. Алгоритм не берётся
из заголовка токена. Каждый авторизованный REST-запрос дополнительно проверяет
сессию в PostgreSQL: отзыв применяется без ожидания истечения access.

Refresh: случайный 32-byte token, в БД только SHA-256 digest. Семья = session.
Абсолютный срок семьи 14 дней, ротация его НЕ продлевает. Сохраняется история
consumed digests для обнаружения reuse. По умолчанию не более 4096 токенов
в семье; превышение отзывает семью с refresh_limit. Это защита от бесконечного
роста истории, а не рекомендация ротировать токен при каждом API-запросе.

Все изменения сессий одного аккаунта берут user row lock. Порядок блокировок:
user -> session -> token. Конкурентные задачи используют отдельные AsyncSession.
Повтор consumed token: commit отзыва семьи, затем 401 refresh_reused.

ВАЖНО: при двух одновременных refresh с ОДНИМ токеном первый получает новую
пару, второй обнаруживает reuse и отзывает всю семью, включая выданную пару.
Это намеренная строгая политика. Будущий desktop обязан сериализовать refresh.
При потере успешного refresh-ответа нельзя слепо повторять старый token: потребуется
повторный вход. Границы сессии нельзя ослаблять фиктивной идемпотентностью.
Текущий архив не реализует desktop mutex и защищённое хранение токенов ОС.

Максимум 10 активных сессий: новый login отзывает старейшую сверх лимита.
Смена/административный reset пароля отзывает все сессии, включая текущую.
Истечение access не должно в будущем обрывать медиа: WSS reauth и media lifecycle
пока отсутствуют, их нельзя подменять REST-аутентификацией на каждый пакет.

## Rate limits

Реальный Redis Lua INCR+EXPIRE, фиксированные окна, fail-closed:
- register: 5/IP/час;
- login: 60/IP/мин и 12/username/5 мин;
- refresh: 60/IP/мин;
- password: 5/account/5 мин;
- revoke: 60/account/мин.
Идентичности в ключах SHA-256, исходный адрес/username не записываются.
Это не анонимизация против перебора username. Не публиковать Redis.
Окна допускают burst у границы. Username-квота может быть использована для
временной блокировки попыток входа конкретного имени; это известный компромисс,
не окончательная anti-abuse система. Redis должен использовать noeviction,
иначе потеря rate keys ослабляет лимиты. Отказ Redis возвращает 503, не обходит guard.

## Admin CLI (из backend/)

```bash
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli reset-password alice
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli cleanup
```

Пароль вводится через getpass с подтверждением, не аргумент командной строки.
Cleanup удаляет до 1000 семей за запуск, если они expired/revoked более 7 дней
назад; digests удаляются каскадно. Нужен периодический запуск, cron пока не добавлен.

## Tests (из каталога с backend/)

```bash
bash scripts/check-auth-unit.sh
export ZVONILKA_TEST_DATABASE_URL='postgresql+asyncpg://zvonilka:TEST_PASSWORD@127.0.0.1:5432/zvonilka_test'
export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
bash scripts/check-auth-integration.sh
```

Unit: identity, schemas, реальные Argon2 операции/worker thread, HTTP boundary,
JWT negative cases, Settings. PG: реальные блокировки/unique constraint,
refresh race, commit reuse revocation, cap history, password/reset/logout,
изоляция пользователей, expiry и session limit. Migration test запускает upgrade
на чистой схеме дважды и проверяет версию/таблицы. API tests используют настоящий
PG/Redis и ASGI transport, без TCP/TLS. Rate limiter также настоящий Redis.

Service tests используют metadata.create_all в изолированных схемах; они НЕ
заменяют отдельный Alembic test. Redis API test keys имеют случайный prefix и
удаляются адресно, без FLUSHDB. Schema cleanup зарегистрирован в addAsyncCleanup.

## Pylance и Ruff

Открой приложенный zvonilka-backend.code-workspace или backend/ как папку проекта,
затем выбери backend/.venv в Python: Select Interpreter. backend/pyrightconfig.json
добавляет src. Если открыть внешнюю папку репозитория, вложенный pyrightconfig
может не стать активным проектом. Это не решается комментариями type: ignore.

Предыдущую строку gather можно переписать без генераторного narrowing:
```python
for result in results:
    if isinstance(result, dict):
        continue
    self.assertIsInstance(result, RoomError)
    if not isinstance(result, RoomError):
        self.fail("unexpected result type")
    self.assertEqual(result.code, "room_limit")
```
assertIsInstance не всегда сужает тип для анализатора; явный isinstance сужает.
Предыдущие тесты этим архивом не заменяются.

Ruff formatter не заменяет ruff check или Pylance. Полный lint/typecheck здесь
не запускался. Команды при установленном ruff:
```bash
uv run ruff check src/zvonilka/auth src/zvonilka/api.py src/zvonilka/config.py src/zvonilka/db.py src/zvonilka/http_boundary.py src/zvonilka/server.py
uv run ruff format src tests
```

## Граница этапа

Нет связки REST комнат с аккаунтами, WSS, WebRTC, Electron, TURN/DSP/helper,
server deployment, CI, метрик и production эксплуатационных сценариев.
Этот архив — функциональная auth-реализация для проверки, не готовая звонилка.

## Фактически выполнено при подготовке

55 новых tests обнаружено: 21 прошло, 34 пропущено из-за отсутствия сервисов
или зависимостей. Все пропуски явно перечислены в STEP-4-TEST-RESULTS.txt.
Старые 31 Redis tests прошли по присланному пользователем выводу; здесь не
перезапускались. Ни одна новая PostgreSQL/JWT/API проверка не выдаётся за пройденную.
