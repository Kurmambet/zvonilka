#!/usr/bin/env bash
set -euo pipefail
: "${ZVONILKA_TEST_REDIS_URL:?Set URL of a dedicated test Redis instance}"
cd "$(dirname "$0")/../backend"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
python -m unittest discover -s tests -p test_redis_integration.py -v
