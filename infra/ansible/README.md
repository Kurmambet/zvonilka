# сопоставление сводки с клиента с логами:

```bash
docker compose --env-file .env.compose.staging -f compose.staging.yaml logs --no-color api \
 | grep -F 'generation='
```

### ну или так:

```
dc() { docker compose --env-file .env.compose.staging -f compose.staging.yaml "$@"; }
dc config --quiet

dc logs --no-color api | grep -F 'generation='

dc logs --tail=200 api
```

# передеплой до cicd:

```bash
(cd zvonilka/desktop && npm run typecheck && npm run lint)





VPS=root@IP_хоста
REV="$(git rev-parse HEAD)"
IMAGE="zvonilka-backend:$REV"

docker build --platform linux/amd64 \
  -f zvonilka/backend/Dockerfile \
  -t "$IMAGE" .


# после сборки


set -o pipefail

docker image save "$IMAGE" | gzip -1 | ssh "$VPS" 'docker image load' &&
printf 'ZVONILKA_STAGING_IMAGE=%s\n' "$IMAGE" > release.env &&
scp compose.staging.yaml compose.staging.vps.yaml release.env \
  "$VPS:/opt/zvonilka-staging/"
```

### Дальше на впс:

```bash
# проверка, что .env есть
cd /opt/zvonilka-staging

grep -oE '^ZVONILKA_TURN_(HOST|SECRET)=' shared/compose.env



dc() {
  docker compose -p zvonilka-staging \
    --env-file shared/compose.env \
    --env-file release.env \
    -f compose.staging.yaml \
    -f compose.staging.vps.yaml "$@"
}

image="$(sed -n 's/^ZVONILKA_STAGING_IMAGE=//p' release.env)"
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' "$image"
dc config --quiet && dc config --images

# Проверь, что образ - linux/amd64, а список содержит новый SHA для api и migrate.


dc up -d --no-build &&
dc ps -a &&
curl --max-time 5 -fsS http://127.0.0.1:8002/health/ready


# с хоста (Ожидаем 401):
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  https://api.zvonilka.website/api/v1/ice-servers
```

# Применить новый ансибл

```bash
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --limit swiss-vps --ask-pass --list-hosts
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --limit swiss-vps --ask-pass --syntax-check
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --limit swiss-vps --ask-pass

# с выпуском сертификатов (если с ними чтото не так)
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --ask-pass   -e '{"edge_issue":true,"edge_email":"pitr.vishnyakovv.05@gmail.com"}'


# поднять turn
ansible-playbook -i edge-vps.ini turn-zvonilka.yml --syntax-check
ansible-playbook -i edge-vps.ini turn-zvonilka.yml   --limit swiss-vps --ask-pass --ask-vault-pass   -e @turn-vault.yml


# применить несколько плейбуков
ansible-playbook -i edge-vps.ini zvonilka-vps.yml \
  --limit new-vps --ask-pass --ask-vault-pass

# new-vps нужно добавить в группы, на которые нацелены соответствующие playbook’и
```

## Как посмотреть, что можно запустить

```bash
# Какие хосты входят в какие группы
ansible-inventory -i edge-vps.ini --graph

# Кого затронет конкретный playbook и какие задачи объявлены
ansible-playbook -i edge-vps.ini edge-zvonilka.yml \
  --limit swiss-vps --list-hosts --list-tasks

# Какие варианты выборочного запуска уже описаны тегами
ansible-playbook -i edge-vps.ini edge-zvonilka.yml \
  --limit swiss-vps --list-tags
```
