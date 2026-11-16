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

## Проверено в среде автора

Проверен синтаксис sender, router и двух тестовых файлов. Patch прошёл --check,
применён, результат побайтово сравнен с подготовленным router.py. ZIP проверен.

Runtime-тесты в этой поставке НЕ запускались. Ruff/Pylance также НЕ запускались.
В среде нет FastAPI/Starlette и полного установленного backend-окружения.
9 новых тестов переданы для пользовательского запуска, а не объявлены пройденными.
Прежние 17 тестов Outbox подтверждены пользователем до этой интеграции, но это
не доказательство, что новые пути WS уже прошли проверки.
