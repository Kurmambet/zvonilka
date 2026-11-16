# Поставка 4.2: подключение Outbox к текущему WS

Предусловие: применена поставка presence и уже добавлен проверенный outbox.py
из поставки 4.1. Зависимости не меняются. Этот архив НЕ заменяет outbox.py.
События участников, room coordinator и SDP/ICE пока не добавляются.

## Применение

Распаковать ZIP в корень проекта. Добавляются sender.py и два файла тестов.
Patch изменяет ТОЛЬКО signaling/router.py. Исходник восстановлен из присланного
signaling-current.md + presence patch с нормализованными окончаниями файлов;
проверка git apply выполнена на этой базе. Форматирование/семантику outbox.py
после пользовательского запуска здесь не перезаписываем.

Из корня проекта:

```bash
git apply --check signaling-writer.patch
git apply signaling-writer.patch
```

При отказе --check остановиться; не применять принудительно и не менять проект
под patch. Новые файлы при этом можно оставить, но тесты запускать после применения.

Из zvonilka/backend/:

```bash
uv run ruff check --select I --fix src/zvonilka/signaling tests/test_signaling_writer*.py
uv run ruff format src/zvonilka/signaling tests/test_signaling_writer*.py
uv run ruff check src/zvonilka/signaling tests/test_signaling_writer*.py
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_outbox.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_writer.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_writer_transport.py --require-no-skips
```

Новые наборы: 6 unit + 3 ASGI. Redis/PostgreSQL им не нужны.
Затем прежние транспортные, presence и network проверки:

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_transport.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_presence_transport.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_network.py --require-no-skips
```

С установленными тестовыми PostgreSQL/Redis URL дополнительно:

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_integration.py --require-no-skips
```

## Реализовано

После accept запускается SocketWriter, использующий настоящий Outbox из 4.1.
Joined и heartbeat_ack ставятся в его FIFO-очередь. Постановка не означает доставку;
таймаут фактической отправки обеспечивается самим Outbox, а не временем offer().

Fatal error передаётся через final_message, иначе немедленное закрытие Outbox
отбросило бы это ожидающее сообщение. Ошибка/переполнение уже закрывающегося
транспорта не обещает доставку финального JSON. Close code задаёт первый путь
закрытия. Отказы до accept по-прежнему идут напрямую: writer там ещё не создан.

Один monitor ждёт окончания writer. Queue-full/send-timeout сигнализируют
Connection.stop(overloaded), другие transport-ошибки — internal_error. Поэтому
обработчик не продолжает ждать heartbeat после отказа своего отправителя:
прерывает ожидающую операцию, делает generation-aware leave и освобождает слот.

Нормальное завершение writer не превращается в ложную ошибку Connection.
В finally закрытие writer, ожидание monitor и cleanup защищены AnyIO shield.
Монитор ничего не отправляет: send_text и close принятого сокета принадлежат
одной writer-задаче. Исключение — авария до успешного запуска writer, где остаётся
ограниченное прямое закрытие. Жёсткая остановка процесса/некооперативная зависимость
не превращаются в гарантию cleanup.

Финальный путь может занять до остатка send_timeout + final send_timeout +
close_timeout, затем существующий cleanup_timeout. При стандартных значениях
это до 6 + 3 секунд без учёта планирования. Shutdown timeout остался прежним;
не считать эти оценки доказательством завершения при любой неисправности.

Реестр комнат, поколения, heartbeat и Lua не переписывались. Sender пока локален
одному WS; публикация room events и согласование snapshot/events будут следующим
отдельным изменением. Наличие очереди само по себе эти задачи не решает.

# Поставка 4.3: полные снимки online-участников

Это реализация, подключённая к WS, а не отдельная неиспользуемая схема.
Предусловия: приняты presence, outbox 4.1 и writer 4.2 с исправленным отказным
тестом. sender.py, outbox.py, Redis/Lua, API авторизации и зависимости не меняются.

## Применение — без patch

Архив содержит только 9 нужных Python-файлов (6 обновлённых, 3 новых) и эту
инструкцию. Перед заменой сохранить локальные изменения/копии этих файлов.
Распаковать в корень проекта, где находятся pyproject.toml и zvonilka/.

Обновляются:

- src/zvonilka/signaling/{schemas,service,router}.py;
- tests/test_signaling_transport.py;
- tests/test_signaling_network.py;
- tests/test_signaling_integration.py.

Добавляются:

- src/zvonilka/signaling/participants.py;
- tests/test_signaling_participants_unit.py;
- tests/test_signaling_participants_transport.py.

Все пути выше относительно zvonilka/backend/.
Рабочая база восстановлена из signaling-current.md, presence patch и принятой
правки writer 4.2. Завершающие переводы строк нормализованы. Ранее исправленный
отказный test_signaling_writer_transport.py НЕ перезаписывается.
Если после этих поставок самостоятельно меняли семантику перечисленных файлов,
не перезаписывать такие изменения молча: сначала сравнить. Форматирование Ruff
может быть повторено; это не отдельная функциональная правка.

Из zvonilka/backend/:

```bash
uv run ruff check --select I --fix src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff format src/zvonilka/signaling tests/test_signaling_*.py
uv run ruff check src/zvonilka/signaling tests/test_signaling_*.py
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_participants_unit.py --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_signaling_participants_transport.py --require-no-skips
```

Затем регрессия (с существующими тестовыми PostgreSQL/Redis URL, не production):

```bash
uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
uv run python -m zvonilka.testing.runner --start tests --pattern test_rooms_api_integration.py --require-no-skips
```

Без тестовых URL integration будет skipped и --require-no-skips завершится
ошибкой намеренно. Новый код использует штатные существующие зависимости.

## Контракт participants

После собственного joined клиент получает:

```json
{
  "version": 1,
  "type": "participants",
  "room_id": "00000000-0000-4000-8000-000000000001",
  "epoch": "00000000-0000-4000-8000-000000000002",
  "sequence": 1,
  "members": [
    {
      "user_id": "00000000-0000-4000-8000-000000000003",
      "generation": "00000000-0000-4000-8000-000000000004"
    }
  ]
}
```

Это полный снимок ONLINE-подключений, включая самого получателя. Не список
допущенных аккаунтов из Redis и не список присутствий, оставшихся после сбоя
API до lease expiry. Чужих credentials/session ID в нём нет.
Members сортируются по canonical user_id. Размер — не более трёх; user_id и
поколения уникальны. Снимок отправляется после join, замены поколения, detach.

Полные снимки выбраны сознательно для mesh-комнаты максимум на троих. Если
лимит изменится, пересмотреть стоимость рассылки и только тогда вводить дельты.
Короткие комментарии об этом и важных границах оставлены рядом с кодом.

Epoch — UUID локального жизненного цикла комнаты. Sequence начинается с 1
и увеличивается при изменениях локального online-реестра. Эти поля НЕ являются
Redis revision, lifetime или глобальным порядком между комнатами. Первый снимок
нового клиента может иметь sequence > 1. При исчезновении всех участников и
pending joins локальная комната удаляется; следующий вход получает новый epoch.
После рестарта API epoch также новый. Мобильный/desktop-клиент пока не реализован.

## Порядок и гонки

Per-room lock сериализует асинхронные Redis admission/join. Собственный joined
ставится в очередь до публикации первого participants, без await между этими
локальными действиями. Дальше FIFO Outbox сохраняет порядок для каждого WS.
Это гарантия порядка постановки/отправки, а не доказательство получения клиентом:
очередь/транспорт всё ещё могут отказать.

Attach/detach/commit снимка синхронные и не ждут сеть. Поэтому disconnect может
обрабатываться, даже пока другой join ожидает Redis; room.users сохраняет room
и epoch, пока есть такой pending join. Публикация новому составу выполняется после
успешного admission. При отказе admission ложный online-участник не добавляется.

Cleanup проверяет поколение в локальном реестре, прежде чем удалить участника.
Поздний disconnect старого окна не удаляет новое и не создаёт лишний snapshot.
Локальный detach выполняется до ожидания закрытия writer и Redis leave. Ошибка
очереди одного подписчика сигнализирует его Connection.stop, а не блокирует
остальные очереди. Следующий detach публикует оставшийся состав.

Состояние комнат/leases и допуск остаются в Redis. Hub хранит только живые
подключения одного процесса. Несколько workers по-прежнему не поддерживаются.
Новых бесконечных фоновых задач нет; sender/monitor используют прежний lifecycle.

## Изменение тестового транспорта

WsSession.receive_wire() читает следующее фактическое сообщение без фильтрации.
Новые ASGI-проверки используют именно его для joined -> participants и дальнейших
снимков. Обычный receive() для старых request/response-тестов теперь валидирует
каждый participants, проверяет монотонность sequence и постоянство epoch на живом
WS, сохраняет модели в participants_messages и только затем ждёт ответ операции.
Неверные snapshots не скрываются. Порядок проверяется отдельно через receive_wire.
Network-тесты также валидируют служебные снимки перед ожиданием ack/закрытия.

Добавлен настоящий PostgreSQL/Redis integration test двух допущенных аккаунтов:
порядок первых сообщений, одинаковый снимок, поколения и удаление при disconnect.
Это не подмена реального admission mock-методом.
