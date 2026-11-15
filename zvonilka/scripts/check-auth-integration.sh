#!/usr/bin/env bash
set -euo pipefail
: "${ZVONILKA_TEST_DATABASE_URL:?Set postgresql+asyncpg URL of a dedicated test database}"
: "${ZVONILKA_TEST_REDIS_URL:?Set URL of a dedicated test Redis}"
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
for pattern in test_auth_postgres.py test_auth_migrations_postgres.py test_auth_api_integration.py; do
  env -u VIRTUAL_ENV uv run python -m zvonilka.testing.runner \
    --start tests --pattern "$pattern" --require-no-skips "$@"
done
