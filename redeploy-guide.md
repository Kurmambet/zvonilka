Да, порядок верный: **собрать на Mint → загрузить образ на VPS → обновить Compose и тег → запустить без сборки → проверить API**. Блок с переносом TURN-секрета нужен **один раз**, а не при каждом релизе.

И да, **Ansible может делать этот деплой вместо тебя**. Сейчас он настраивает Nginx, Certbot и coturn, потому что для них мы написали отдельные плейбуки. Деплой готового образа пока остался ручным: старый плейбук собирал образ прямо на VPS, а мы сознательно от этого ушли. Для этого релиза можно использовать уже проверенный путь, а следующим изменением вынести передачу образа, доставку файлов, `up --no-build` и проверку готовности в отдельный `deploy-zvonilka.yml`. Сборку можно оставить отдельным шагом на Mint или запускать из Ansible локально — **не на VPS**. [docs.ansible](https://docs.ansible.com/projects/ansible/latest/collections/community/docker/docker_compose_v2_module.html)

## 1. На Mint: проверить, закоммитить и собрать

Перед `REV="$(git rev-parse HEAD)"` убедись, что **TURN-правки уже в коммите**. Иначе Docker соберёт файлы из рабочего дерева, а тег будет указывать на старый коммит — по нему нельзя будет точно понять, что работает на сервере.

```bash
git diff --check
git status --short
(cd zvonilka/desktop && npm run typecheck && npm run lint)
```

Если правки ещё не закоммичены, сначала закоммить их **без env-файлов и секретов**. Если проверки Electron упали, остановись до деплоя и разберись с ошибкой. Затем из корня репозитория, в Bash:

```bash
VPS=root@2.56.119.52
REV="$(git rev-parse HEAD)"
IMAGE="zvonilka-backend:$REV"

docker build --platform linux/amd64 \
  -f zvonilka/backend/Dockerfile \
  -t "$IMAGE" .
```

Только после успешной сборки:

```bash
set -o pipefail

docker image save "$IMAGE" | gzip -1 | ssh "$VPS" 'docker image load' &&
printf 'ZVONILKA_STAGING_IMAGE=%s\n' "$IMAGE" > release.env &&
scp compose.staging.yaml compose.staging.vps.yaml release.env \
  "$VPS:/opt/zvonilka-staging/"
```

Повторять `ssh ... 'whoami'` и `install -d` не нужно: SSH уже проверен, каталог существует. **Обновлённый `compose.staging.yaml` обязательно передай** — именно он передаёт TURN-переменные в контейнер. `shared/compose.env` с Mint не копируем.

## 2. На VPS: добавить TURN-настройки, если их ещё нет

В `/opt/zvonilka-staging` можешь использовать свой Python-блок без изменений. Выполняй его только если обе записи ещё отсутствуют в `shared/compose.env`. Если уже добавил их прошлым шагом, пропусти блок. Проверить наличие ключей, не выводя значения:

```bash
cd /opt/zvonilka-staging

grep -oE '^ZVONILKA_TURN_(HOST|SECRET)=' shared/compose.env
```

Должны быть обе строки: `ZVONILKA_TURN_HOST=` и `ZVONILKA_TURN_SECRET=`. Эта проверка показывает **наличие**, не правильность значений. Если есть только одна строка, не добавляй дубликаты: исправь недостающую запись отдельно. Твой блок уже выставляет `0600`, поэтому повторный `chmod` не обязателен. Пароль БД, JWT-секрет и Vault не меняем.

## 3. На VPS: запустить новый релиз и проверить его

Если это новая SSH-сессия, заново определи `dc`:

```bash
cd /opt/zvonilka-staging

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
```

Проверь, что образ — `linux/amd64`, а список содержит **новый SHA** для `api` и `migrate`. Если всё совпало:

```bash
dc up -d --no-build &&
dc ps -a &&
curl --max-time 5 -fsS http://127.0.0.1:8002/health/ready
```

Compose пересоздаст сервисы, для которых изменился образ или конфигурация, но не будет собирать образ на VPS. Если `curl` сработал раньше, чем API успел стать готовым, дождись `healthy` в `dc ps -a` и повтори проверку. Ожидаем `migrate Exited (0)` и `{"status":"ready"}`. Не запускай `down`: для этого обновления он не нужен. Nginx, coturn и Amnezia не трогаем. [docs.docker](https://docs.docker.com/reference/cli/docker/compose/up/)

С Mint проверь, что **новый маршрут** доступен через HTTPS и требует авторизацию:

```bash
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' \
  https://api.zvonilka.website/api/v1/ice-servers
```

Ожидаем `401`, не `404`. Это проверяет маршрут и защиту, но **не выдачу TURN-данных**. Её проверяем двумя обновлёнными Electron-клиентами: включаем «Только TURN» до подключения к собеседнику и смотрим, что выбрана пара `relay → relay`, RTP идёт в обе стороны и звук слышен у обоих.
