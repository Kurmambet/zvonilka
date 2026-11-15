set -euo pipefail
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
for pattern in test_rooms_unit.py test_rooms_schemas.py test_rooms_config.py; do
  uv run python -m zvonilka.testing.runner \
    --start tests --pattern "$pattern" --require-no-skips "$@"
done
