#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
for name in identity schemas passwords http_boundary jwt config; do
  env -u VIRTUAL_ENV uv run python -m zvonilka.testing.runner \
    --start tests --pattern "test_auth_${name}.py" --require-no-skips "$@"
done
