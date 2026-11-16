# 4.5b1 — контракт reauthenticate и правила сроков авторизации

## ВАЖНО: это не включение повторной авторизации на живом WS

Поставка содержит четыре НОВЫХ файла. Существующие router/service/schemas,
auth, Redis, Outbox и тестовые помощники не меняются. В частности, текущий
parse_active пока по-прежнему отклоняет reauthenticate. Не отправлять эти
сообщения рабочему WS до следующей интеграционной поставки.

Это небольшая проверяемая часть: зафиксировать wire-контракт и детерминированные
правила времени прежде, чем добавлять таймеры/задачи в рабочий handler.

## Содержимое (относительно zvonilka/backend/)

- src/zvonilka/signaling/auth_protocol.py;
- src/zvonilka/signaling/authorization.py;
- tests/test_signaling_auth_protocol.py — 8 тестов;
- tests/test_signaling_authorization.py — 13 тестов.

## Контракт

Клиентское сообщение:

```json
{
  "version": 1,
  "type": "reauthenticate",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "generation": "00000000-0000-4000-8000-000000000002",
  "access_token": "new-access-token-from-http-refresh"
}
```

Успешный ответ (поколение не меняется):

```json
{
  "version": 1,
  "type": "reauthenticated",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "generation": "00000000-0000-4000-8000-000000000002",
  "expires_at": 1800000000
}
```

Уведомление после истечения JWT:

```json
{
  "version": 1,
  "type": "reauth_required",
  "generation": "00000000-0000-4000-8000-000000000002"
}
```

expires_at — UTC Unix seconds из проверенного exp, а не время выпуска нового JWT.
Access token в модели SecretStr: стандартные repr/model_dump_json скрывают его,
для реальной клиентской отправки требуется явно взять настоящий секрет. При этом
validation exception всё ещё может содержать исходный input: не логировать его.
Refresh token, user_id, session_id, room_id и клиентский expires_at запрещены.
Серверная идентичность берётся только из проверенного AuthService результата.
В этой части используются отдельные adapters; общие adapters рабочего WS пока
намеренно не расширены, чтобы не объявить поддержку неготовой команды.

## AuthorizationWindow

Чистая модель, без JWT-парсинга, БД, сети, sleep и фоновых задач.
Конструктор и reauthenticated вызываются ТОЛЬКО с данными, уже проверенными
AuthService.authenticate_access(). Модель сама не доказывает подлинность токена.

Идентичность привязана к тем же user_id И session_id. Поколение и presence
здесь не хранятся и не меняются: их fencing остаётся задачей интеграционного слоя.

Состояния:
- authorized: now < verified expiry deadline;
- reauth_required: expiry <= now < expiry + 30 seconds;
- expired: now >= expiry + 30 seconds; оживить такое окно нельзя.

Период проверки сессии по умолчанию 10 секунд. session_check_due сообщает о сроке,
но не обращается в БД. session_checked отмечает УСПЕШНУЮ проверку: новый JWT не
выпускается, exp и grace не сдвигаются. Проверки допустимы и во время grace;
после окончательного expiry проверку нельзя использовать для оживления окна.
Ошибки/отзыв сессии будущий WS handler должен переводить в остановку соединения,
а не в session_checked. Таким образом 10 секунд — период, а не самостоятельная
гарантия обнаружения с нулевым временем DB-запроса: его timeout учитывается отдельно.

Срок exp переводится из wall clock в monotonic deadline. Для каждого обращения
передавать свежий monotonic now, а не timestamp, захваченный до какого-либо await.
Уход monotonic времени назад и нечисловые/бесконечные интервалы отклоняются.

Новый проверенный grant может восстановить authorized в пределах grace. Другая
учётная запись/сессия, уже истёкший токен и откат к более старому exp отклоняются.
Одинаковый exp не увеличивает прежний monotonic deadline даже при переводе
системных часов назад и не открывает уже начавшийся grace повторно.
next_deadline выбирает ближайшую проверку сессии или границу состояния.
Если он вернул now, интеграционный слой обязан обработать событие, а не busy-loop.

Эта модель пока НЕ приостанавливает SDP/ICE и НЕ разрешает heartbeat в grace:
это утверждённая политика следующего рабочего интеграционного патча, не результат
уже подключённого к handler поведения. Не путать чистые state-тесты с ASGI-проверкой.

## Применение

Сохранить свои изменения. Из корня репозитория, содержащего zvonilka/backend/:

```bash
git apply --check signaling-reauth-contract.patch
git apply signaling-reauth-contract.patch
cd zvonilka/backend
uv run ruff check --select I --fix src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff format src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff check src/zvonilka/signaling tests/test_signaling_*.py
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_auth_protocol.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_authorization.py --require-no-skips
```

Затем с существующими тестовыми PostgreSQL/Redis URL:

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
```

21 новый тест + принятые 163 = ожидается 184. Это размер набора, не объявление
его успешного прогона автором. Старые тесты не переписываются и не отключаются.

## Фактически проверено автором

- новые protocol: 8/8;
- новые authorization: 13/13;
- прежние relay unit из принятого patch: 26/26;
- прежние schemas: 18/18.

Итого 65 passed, без skips, Python 3.12.8. Новые тесты не используют реальные
часы/таймеры, не подменяют PyJWT/FastAPI и не устанавливают фальшивые runtime-модули.
Проверены ast.parse всех четырёх файлов, git apply --check, применение и точное
сравнение результата. Patch добавляет только новые файлы. Проверена целостность ZIP.

Ruff/Pylance, весь signaling-набор, ASGI/TCP и БД здесь НЕ запускались.
В среде автора нет полного runtime. Уже пройденный пользователем 4.5a не меняется.

Следующая поставка — именно подключение этих правил к рабочему WS:
initial authenticated metadata, reauthenticate, уведомление, grace, остановка
relay в обе стороны, DB session watchdog и cleanup. Отдельных новых подсистем
ради этого не планируется. REST-close и финальная приёмка остаются после неё.
