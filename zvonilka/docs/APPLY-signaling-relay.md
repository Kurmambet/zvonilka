# Поставка 4.4 — адресная пересылка SDP/ICE

Основа: пользовательский signaling-before-relay.md после принятой 4.3.
Один процесс API, одна реплика. Никаких изменений auth/Redis/Lua/REST, Outbox,
SocketWriter, test_signaling_network.py и старых тестов. Новых зависимостей нет.

## Применение

Сохранить текущие изменения проекта. Распаковать архив с patch и этой инструкцией
в корень репозитория — каталог, содержащий zvonilka/backend/.
Patch меняет только четыре runtime-модуля и добавляет три тестовых файла.
Если git apply --check не проходит, НЕ применять с --reject/force: сначала сравнить
с присланным снимком. Обычная проверка patch не меняет рабочее дерево.

Из корня репозитория:

```bash
git apply --check signaling-relay.patch
git apply signaling-relay.patch
cd zvonilka/backend
uv run ruff check --select I --fix src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff format src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff check src/zvonilka/signaling tests/test_signaling_*.py
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_relay_unit.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_relay_transport.py --require-no-skips
```

Затем с существующими тестовыми PostgreSQL/Redis URL (не production):

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_relay_integration.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_rooms_api_integration.py --require-no-skips
```

Новых тестов 34: 26 unit/schema, 7 ASGI с doubles и 1 реальный integration.
Если прежний набор содержит 129, ожидаемый общий размер — 163 теста, без skips.
Фактический результат полного прогона пока НЕ подтверждён.
Uvicorn legacy WebSocket warning намеренно не меняется.

## Клиентские сообщения после joined

```json
{
  "version": 1,
  "type": "offer",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "generation": "00000000-0000-4000-8000-000000000002",
  "target_user_id": "00000000-0000-4000-8000-000000000003",
  "target_generation": "00000000-0000-4000-8000-000000000004",
  "sdp": "v=0\r\nopaque-test-description"
}
```

Answer имеет те же поля, но type=answer. Для ice_candidate вместо sdp:

```json
{
  "candidate": {
    "candidate": "candidate:1 1 UDP 1 127.0.0.1 5000 typ host",
    "sdp_mid": "0",
    "sdp_mline_index": 0,
    "username_fragment": null
  }
}
```

Это только фрагмент поля candidate; остальные обязательные поля адресного
сообщения остаются как в offer, а type должен быть ice_candidate.
Candidate требует sdp_mid либо sdp_mline_index. Явное завершение сбора:
поле candidate: null. Поле обязательно; отсутствие, пустая строка или пустой
объект не являются завершением. Значения полей в протоколе snake_case.

Generation отправителя берётся из собственного joined; target_generation — из
последнего participants. Room берётся из авторизованного WS, не из сообщения.
User ID отправителя, room_id, from_user_id/from_generation в клиентском сообщении
запрещены. Нельзя сигналить самому себе или автоматически перенаправлять сообщение
со старого target_generation на новое окно.

## Что получает адресат

```json
{
  "version": 1,
  "type": "offer",
  "request_id": "00000000-0000-4000-8000-000000000001",
  "room_id": "00000000-0000-4000-8000-000000000005",
  "from_user_id": "00000000-0000-4000-8000-000000000006",
  "from_generation": "00000000-0000-4000-8000-000000000002",
  "target_generation": "00000000-0000-4000-8000-000000000004",
  "sdp": "v=0\r\nopaque-test-description"
}
```

From-поля формирует сервер из Attempt. Получатель сверяет собственное поколение и
поколение отправителя с текущим составом комнаты. Третий участник не получает
чужие SDP/ICE. Снимки participants и адресные сообщения идут через одну FIFO
конкретного соединения. Request_id — корреляция, не idempotency key: повторные
валидные сообщения не дедуплицируются сервером.

## Ошибки и предел гарантии

Нефатальный error с исходным request_id:

- peer_unavailable — адресата нет в этой комнате, он остановлен или lease-window
  локального соединения истёк. Существование аккаунта в другой комнате не раскрывается;
- peer_replaced — online-участник уже имеет другое поколение;
- peer_overloaded — очередь адресата не приняла сообщение; адресат получает stop,
  а соединение отправителя не закрывается только из-за чужой очереди;
- invalid_target — адресовано самому себе;
- message_too_large — после добавления серверных метаданных выходной frame слишком большой.

Успешная постановка не возвращает ack и НЕ означает получения клиентом. После
постановки могут произойти send timeout, отказ транспорта или disconnect. Нет
буфера offline-доставки, повторной отправки, delivery receipt и гарантии exactly-once.

Поддельные/лишние поля, неправильная структура, устаревшее собственное поколение,
истёкшее окно heartbeat и превышение собственного rate limit — фатальные ошибки
по обычному lifecycle WS. Большой входной frame также остаётся фатальной ошибкой.

## Ограничения и комментарии для дальнейшего развития

- Общий frame по умолчанию <=16384 UTF-8 bytes. SDP <=16384 bytes отдельно, но весь
  входной JSON с его escaping/метаданными и весь серверный JSON должны также
  помещаться в frame limit. Допустимый SDP поэтому может быть короче отдельного лимита.
- Candidate string <=2048 UTF-8 bytes; sdp_mid <=64 символов,
  username_fragment <=256 символов; sdp_mline_index — строго int 0..65535, не bool.
- SDP и ICE содержимое opaque: сервер ограничивает структуру/размер, но не проверяет
  грамматику SDP, состояние WebRTC negotiation, соответствие answer offer и glare.
- На WS отдельный token bucket: sustained 32 relay messages/sec, burst 64.
  Учитываются offer, answer, ICE и валидные запросы к недоступным адресатам.
  При превышении — фатальный rate_limited. Значения — Policy.relay_rate/relay_burst.
  Bucket обновляется по monotonic, без фоновых задач/таймеров. Heartbeat в него не входит.
- Пересылка не продлевает heartbeat deadline и не вызывает Redis heartbeat.
- Наличие отправителя проверяется в service gate и ParticipantsHub. Адресат должен
  быть текущим, не stopped, не expired. Проверка локальной готовности не заменяет
  немедленную auth revocation/REST-close: они остаются за рамками этой поставки.
- Lookup, проверка поколений и enqueue в Hub выполняются без await. Это локальная
  гарантия на момент постановки, не распределённая транзакция с Redis и не гарантия
  времени фактического получения клиентом. Multi-worker требует отдельного проекта.
- Поля SDP/candidate исключены из repr моделей; обычный model_dump/model_dump_json
  всё ещё содержит payload для передачи. Нельзя писать их или validation exceptions
  с исходными значениями в логи. Новые parse failures наружу дают только bounded code.

Короткие комментарии об opaque payloads, доверенной идентичности, token bucket,
размере выходного envelope, обязательном heartbeat и однопроцессной атомарности
оставлены непосредственно в коде. Имеющиеся комментарии participants сохранены.

## Изменённые файлы

- src/zvonilka/signaling/schemas.py — направленные request/server модели и adapters;
- src/zvonilka/signaling/participants.py — ready callback и синхронная адресная доставка;
- src/zvonilka/signaling/service.py — parse_active, fencing, bucket, построение envelope;
- src/zvonilka/signaling/router.py — разделение heartbeat/relay после joined.

Добавлены tests/test*signaling_relay*{unit,transport,integration}.py.
Sender/Outbox и ВСЕ существующие тесты остаются неизменными.
Старый тест no_offer_forwarding всё ещё проверяет МАЛФОРМИРОВАННЫЙ offer без
обязательных полей; он не используется как доказательство новой пересылки.
