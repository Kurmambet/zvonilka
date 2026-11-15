```
export ZVONILKA_TEST_DATABASE_URL='postgresql+asyncpg://zvonilka:пароль@127.0.0.1:5432/zvonilka_test'

export ZVONILKA_TEST_REDIS_URL='redis://127.0.0.1:6379/15'

PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli reset-password alice
PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.auth.cli cleanup


PYTHONPATH=src env -u VIRTUAL_ENV uv run python -m zvonilka.server
```
