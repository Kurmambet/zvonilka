set -euo pipefail
: "${ZVONILKA_TEST_DATABASE_URL:?Set URL of a dedicated PostgreSQL test database}"
: "${ZVONILKA_TEST_REDIS_URL:?Set URL of a dedicated Redis test instance/database}"
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
uv run python -m zvonilka.testing.runner \
  --start tests --pattern test_rooms_api_integration.py --require-no-skips "$@"
