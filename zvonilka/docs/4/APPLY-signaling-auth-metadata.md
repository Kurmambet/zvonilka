# Поставка 4.5a: проверенные auth-данные для будущего WS reauthenticate

Это первый малый патч после принятой 4.4. Он не включает таймеры и новые
WS-сообщения. Поведение живых соединений пока остаётся прежним. Политика
30 секунд grace и 10 секунд проверки сессии будет подключена отдельно.

## Содержимое

Изменены два runtime-файла:

- src/zvonilka/auth/jwt_tokens.py;
- src/zvonilka/auth/service.py.

Добавлены два тестовых файла:

- tests/test_auth_access_metadata.py — 8 unit-тестов на настоящем PyJWT;
- tests/test_auth_access_metadata_postgres.py — 8 сценариев на настоящем PostgreSQL.

Все пути здесь относительно zvonilka/backend/. Новых зависимостей, таблиц,
миграций, настроек, Redis-команд, фоновых задач и изменений signaling нет.

## Что добавлено

AccessTokens.decode_claims(token) -> VerifiedAccessClaims:

- user_id, session_id, expires_at;
- expires_at — целое значение exp из проверенного JWT, UTC Unix seconds;
- сначала выполняются прежние проверки подписи, алгоритма, issuer, audience,
  назначения, обязательных claims и времени; payload не читается с verify=False;
- исходный токен не сохраняется в результате;
- прежний decode(token) по-прежнему возвращает tuple из двух UUID.

AuthService.authenticate_access(token) -> AuthenticatedAccess:

- возвращает Principal и expires_at;
- проверяет JWT и ту же сессию в PostgreSQL;
- после DB await ещё раз сверяет exp со временем: медленный запрос не должен
  вернуть уже просроченное право доступа;
- authenticate(token) сохраняет прежний внешний результат Principal.

AuthService.check_session(user_id, session_id) -> Principal:

- проверяет существование и принадлежность сессии пользователю;
- отклоняет disabled user, revoked или expired session;
- не выпускает JWT, не обновляет срок JWT/сессии и не выполняет refresh;
- предназначен только для уже установленной сервером идентичности. Это не
  альтернативный способ логина и не новый публичный HTTP/WS endpoint;
- DB-запрос и условия проверки перенесены из прежнего authenticate без ослабления;
- ошибки зависимости не превращаются в успешную проверку или invalid_access.

Два результата являются frozen/slots dataclasses и не содержат access/refresh token.
Principal и REST profile не расширяются полем session expiry или сырыми claims.
Короткие комментарии поясняют trusted identity, exp и проверку после DB await.

## Применение

Сохранить свои изменения. Распаковать patch и эту инструкцию в корень репозитория,
содержащий zvonilka/backend/. Основа auth-файлов — переданный project_structure.md;
принятые 4.1–4.4 auth-файлы не меняли. Если их самостоятельно меняли с тех пор,
обязательно сначала проверить применимость. При конфликте не применять принудительно
и не использовать --reject: прислать текущие два auth-файла для пересборки patch.

Из корня репозитория:

```bash
git apply --check signaling-auth-metadata.patch
git apply signaling-auth-metadata.patch
cd zvonilka/backend
uv run ruff check --select I --fix src/zvonilka/auth tests/test_auth_access_metadata*.py
uv run ruff format src/zvonilka/auth/jwt_tokens.py src/zvonilka/auth/service.py tests/test_auth_access_metadata*.py
uv run ruff check src/zvonilka/auth tests/test_auth_*.py
uv run python -m zvonilka.testing.runner --start tests --pattern test_auth_access_metadata.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_auth_access_metadata_postgres.py --require-no-skips
```

Для PostgreSQL-тестов нужен прежний ZVONILKA_TEST_DATABASE_URL на тестовую БД.
Пароль/URL в ответ не присылать. У новых unit-тестов нет skip-маски для отсутствующего
PyJWT: производственная зависимость должна быть установлена через проектное uv-окружение.

Регрессия (для полного набора auth/signaling нужны прежние тестовые PG/Redis URL):

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_auth_*.py' --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
```

Новый signaling пока не добавляется: ожидается прежний размер 163.
Это ожидаемое количество, НЕ результат запуска автором.

## Следующая граница

После приёмки auth-основы: небольшой патч контракта/WS lifecycle с reauthenticate,
привязкой к тем же user/session, дедлайном exp, grace 30 секунд, проверкой отзыва
раз в 10 секунд и обязательной очисткой задач. При необходимости этот патч будет
разделён до реализации, без добавления незаявленной функциональности.

REST-закрытие комнаты и итоговая приёмка signaling остаются отдельными поставками.
Ни этот патч, ни серверные тесты не означают готовность реального медиазвонка.
