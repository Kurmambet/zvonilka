# оформление

```bash
uv run ruff check .

uv run ruff format .

uv run python -m zvonilka.testing.runner --start tests --pattern 'test_signaling_*.py' --require-no-skips
```

# Redis

```bash
sudo apt update
sudo apt install redis-server

sudo systemctl start redis-server
sudo systemctl status redis-server
redis-cli ping


ss -ltn '( sport = :6379 )'

redis-cli CONFIG GET bind
redis-cli CONFIG GET protected-mode

```

# работа с compose

```bash
docker compose --env-file .env.compose.local -f compose.dev.yaml up --build -d


dc() { docker compose --env-file .env.compose.local -f compose.dev.yaml "$@"; }
dc config --quiet

dc up -d
dc up --build -d
dc up -d --no-deps --force-recreate api

dc ps
dc ps -a
dc logs --tail=80 migrate api


dc down
dc down -v
```
