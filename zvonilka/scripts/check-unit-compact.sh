#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
for pattern in test_repository.py test_safe_logging.py test_compact_runner.py; do
  env -u VIRTUAL_ENV uv run python -m zvonilka.testing.runner \
    --start tests --pattern "$pattern" --require-no-skips "$@"
done
