# Этап 3: краткая диагностика и граничные тесты

## Встраивание
Архив — overlay: распаковать там, где находятся backend/, docs/ и scripts/.
Лишней оболочки zvonilka/ нет. pyproject.toml, uv.lock, repository.py и
transaction.lua не заменяются. Новые зависимости не требуются.
Нужен проект этапа 2 для Redis edge tests. 26 новых unit-тестов автономны.

## Команды из каталога с backend/
```bash
bash scripts/check-unit-compact.sh
export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
bash scripts/check-redis-compact.sh
```
Scripts выбирают backend/.venv через uv и убирают унаследованный VIRTUAL_ENV
только для дочерней команды. Внешнее окружение не меняется. Зависимости должны
быть установлены в backend. Это не изменение структуры в uv workspace.

Из backend/ можно запускать напрямую с PYTHONPATH=src:
```bash
PYTHONPATH=src uv run python -m zvonilka.testing.runner --start tests --pattern 'test_redis*.py' --require-no-skips
```

Exit codes: 0 успех, 1 падение/неожиданный успех/запрещённые пропуски,
2 discovery error или ноль найденных тестов, 130 прерывание тестов.
По умолчанию максимум 10 подробностей; длина каждой ошибки ограничена.
Счётчики сохраняют все ошибки. Skipped не входит в passed.
setUpClass failure может дать run=0/errors=1 — это ошибка, не успех.
Ошибки subTest и unexpectedSuccess не теряются.

Для полного локального traceback:
```bash
bash scripts/check-redis-compact.sh --tracebacks
```

Runner не перехватывает произвольные print/stderr из тестируемой программы.
Тексты assertion/exception могут содержать данные теста: не применять реальные
секреты в публичных fixtures. Runner — не production-санитайзер.

## Runtime logging
Это пока модуль, не подключённый к API: исполняемого приложения ещё нет.
Будущий entrypoint должен вызвать configure_logging один раз:
```python
import logging
from zvonilka.observability.logging import configure_logging, log_event
configure_logging()
logger = logging.getLogger("zvonilka")
try:
    await rooms.sweep()
except ConnectionError:
    log_event(logger, "dependency_unavailable", level=logging.ERROR,
              dependency="redis", operation="sweep", code="redis_unavailable",
              exc_info=True)
```
redis-py использует собственные ConnectionError/TimeoutError: реальные границы
сервиса должны обрабатывать и их. Пример не добавляет retry и не имитирует успех.

Вывод: одна JSON-строка, фиксированные event/code/operation/dependency,
валидный UUID request_id, ограниченная duration_ms. Произвольные сообщения,
пароли, токены, URL, IP, user/room ID, extras и текст исключения не выводятся.
Formatter не вызывает getMessage/str(exception)/formatException и игнорирует
cached exc_text/stack_info. Для исключения есть разрешённый тип и error_ref —
хеш позиций стека. Это группировка места ошибки, не уникальный ID события.
Request ID пока предоставляет вызывающий код, middleware ещё нет.

Свободный текст сторонних библиотек заменён общей категорией, иначе нельзя
обещать отсутствие секретов в тексте исключения. Цена — меньше деталей:
нужно добавлять фиксированные коды по мере интеграции, а не сырой текст.
WARNING+ повторы одной категории группируются на 30 секунд. Первая запись
видна сразу, следующая после окна сообщает suppressed_since_last. Финальный
счётчик не выгружается, если событий больше нет; буфер ограничен 256 категориями,
при вытеснении счётчик теряется. Это не точный счётчик метрик и не log rotation.
INFO не подавляются: не писать heartbeat и каждый пакет на INFO.

Для будущего uvicorn: log_config=None и access_log=False. Иначе его handlers
могут обойти formatter и записать URL. configure_logging заменяет только root
handlers; именованные handlers, print, warnings, sys.excepthook, asyncio exception
handler и последующая перенастройка логирования не перехватываются.
Это не обещание, что любое исключение процесса уже обработано.
Полного traceback в production-formatter нет даже на DEBUG.

## Новые реальные Redis-проверки
1. Конкурентное создание не переполняет pending-квоту.
2. Конкурентная выдача ссылок не превышает число свободных мест.
3. Отказ по вместимости не расходует токен.
4. Два разных токена конкурируют за последнее место: допуск получает один.
5. Гонка revoke/redeem согласована при любом порядке операций.
6. Повтор успешного redeem работает после исходного TTL токена.
7. Heartbeat не продлевает максимальную жизнь комнаты.
8. Просроченный heartbeat не воскрешает lease, нужен join.
9. Recovering занимает слот, окончание grace его освобождает.
10. Допуск гостя не даёт прав владельца.

В этой среде Redis отсутствует: эти 10 тестов НЕ выполнены против Redis.
Используются реальные задержки TTL. Не подгонять ожидания под зелёный результат.

## Предыдущие результаты и продолжение
Пользователь прислал вывод этапа 2: 20 тестов за 5.072s, OK на его машине.
Версия Redis и persistence не указаны. Это не проверка crash recovery,
перезапуска Redis, производительности, API/auth или медиа.
Следующий функциональный этап: PostgreSQL, авторизация/refresh и FastAPI
error boundary с подключением этих модулей; здесь они ещё не реализованы.
