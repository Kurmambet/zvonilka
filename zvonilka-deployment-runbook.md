# Zvonilka: журнал первого деплоя на швейцарский VPS

Команды ниже разделены по месту запуска. Диагностические попытки сохранены для истории, а не как обязательные шаги повторного деплоя. На VPS уже работает AmneziaWG (`amnezia-awg2`, `40878/udp`): не переустанавливать хост, не запускать глобальную очистку Docker, не применять старые bootstrap/site-плейбуки. Пароли, JWT-секрет и ACME-данные из журнала не включены.

## 1. Подключение и обследование VPS

С Mint (сверить ключ хоста по доверенному каналу):

```bash
ssh root@2.56.119.52
```

На VPS под root:

```bash
grep '^PRETTY_NAME=' /etc/os-release
uname -m
nproc
uptime
free -h
swapon --show
vmstat 1 10
df -hT / /var /opt
df -ih / /var /opt
ps -eo pid,comm,rss,%mem,%cpu --sort=-rss | head -n 16
journalctl --disk-usage

docker compose version
docker info --format 'Docker root: {{.DockerRootDir}}'
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker stats --no-stream
docker system df
docker ps

ss -lntup
ss -lntup '( sport = :80 or sport = :443 or sport = :8002 or sport = :3478 or sport = :5349 )'
systemctl list-units --type=service --state=running --no-pager
systemctl show nginx -p FragmentPath -p ExecStart --no-pager
nginx -T 2>&1 | less
ss -tulpn | grep ':443'
```

Итог: Ubuntu 26.04, amd64, 1 vCPU, 1.9 GiB RAM (1.4 GiB доступно в замере), без swap, 22 ГБ свободно. Nginx и Compose отсутствовали; 80/443 свободны локально. Docker уже обслуживал VPN.

## 2. Compose: проверка пакета и первая неудачная попытка

```bash
docker version
command -v docker
dpkg -S "$(command -v docker)" 2>/dev/null || true
dpkg-query -W docker.io docker-ce docker-ce-cli containerd containerd.io \
  docker-compose-v2 docker-compose-plugin 2>/dev/null || true
apt-cache policy docker.io docker-ce docker-compose-v2 docker-compose-plugin
apt-get -s install --no-install-recommends docker-compose-v2
systemctl show docker.service -p MainPID -p ExecMainStartTimestamp
docker inspect --format 'VPN: {{.State.Status}}, started={{.State.StartedAt}}, restarts={{.RestartCount}}' amnezia-awg2
apt-get install --no-install-recommends docker-compose-v2
```

План добавлял только `docker-compose-v2`. Первая загрузка упала на `Temporary failure resolving 'archive.ubuntu.com'`.

## 3. История диагностики исходящей сети

```bash
systemctl is-active systemd-resolved
ls -l /etc/resolv.conf
cat /etc/resolv.conf
resolvectl status
timeout 10s resolvectl query archive.ubuntu.com
timeout 10s resolvectl query example.com
timeout 10s getent ahosts archive.ubuntu.com
ip -4 route show default
journalctl -u systemd-resolved -b -n 30 --no-pager
ip -4 route get 1.1.1.1
ip -4 route get 8.8.8.8
for dns in 1.1.1.1 8.8.8.8; do
  dig @"$dns" archive.ubuntu.com A +notcp +ignore +time=3 +tries=1
  dig @"$dns" archive.ubuntu.com A +tcp +time=3 +tries=1
done
curl -4 --noproxy '*' --connect-timeout 4 --max-time 8 -fsS \
  --resolve cloudflare-dns.com:443:1.1.1.1 -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=archive.ubuntu.com&type=A'
curl -4 --noproxy '*' --connect-timeout 5 --max-time 10 -v \
  --resolve dns.google:443:8.8.8.8 \
  'https://dns.google/resolve?name=archive.ubuntu.com&type=A'
```

В первой SSH-сессии, с запросами из второй:

```bash
timeout 20s tcpdump -pni net0 \
  '(host 1.1.1.1 or host 8.8.8.8) and (port 53 or tcp port 443)'
```

Во второй:

```bash
dig @1.1.1.1 archive.ubuntu.com A +notcp +ignore +time=3 +tries=1
ip -4 rule show
ip -4 route show table all
nft list ruleset
iptables -S
```

Уточняющий захват в первой SSH-сессии:

```bash
timeout 40s tcpdump -p -nn -e -l -i net0 \
  '(arp and host 10.0.0.1) or (host 8.8.8.8 and tcp port 443)'
```

Во второй, пока работает захват:

```bash
ip -br -4 addr show dev net0
ip -4 route get 8.8.8.8
ip -4 neigh show to 10.0.0.1 dev net0
printf 'SSH_CONNECTION=%s\n' "$SSH_CONNECTION"
for src in 176.124.207.191 2.56.119.52; do
  ip -4 route get 8.8.8.8 from "$src"
  curl -4 --noproxy '*' --interface "$src" --connect-timeout 4 --max-time 8 -sS \
    --resolve dns.google:443:8.8.8.8 \
    'https://dns.google/resolve?name=archive.ubuntu.com&type=A'
  printf '\n'
done
ip -4 neigh show to 10.0.0.1 dev net0
for dns in 1.1.1.1 8.8.8.8; do
  dig -4 -b 2.56.119.52 @"$dns" archive.ubuntu.com A +notcp +ignore +time=3 +tries=1
  dig -4 -b 2.56.119.52 @"$dns" archive.ubuntu.com A +tcp +time=3 +tries=1
done
networkctl status net0 --no-pager
ip -4 route show default
grep -RnsE '176\.124\.207\.191|2\.56\.119\.52|10\.0\.0\.1' \
  /etc/netplan /etc/systemd/network /run/systemd/network 2>/dev/null
```

Вывод: старый исходный IPv4 давал SYN без ответа; с `2.56.119.52` работали HTTPS к Google и DNS по UDP/TCP. Шлюз был доступен. Правила firewall и VPN не меняли.

### Временный тест маршрута (история, не повторять на уже исправленном хосте)

```bash
ip -4 route show default
systemd-run --unit=zvonilka-route-rollback --on-active=3min \
  "$(command -v ip)" -4 route change default via 10.0.0.1 dev net0 proto static &&
systemctl is-active --quiet zvonilka-route-rollback.timer &&
ip -4 route change default via 10.0.0.1 dev net0 proto static src 2.56.119.52
ip -4 route get 8.8.8.8
timeout 10s getent ahostsv4 archive.ubuntu.com
curl -4 --noproxy '*' --connect-timeout 5 --max-time 12 -fsSI \
  'https://archive.ubuntu.com/ubuntu/dists/resolute/Release'
for dst in 91.189.92.23 185.125.190.83; do
  curl -4 --noproxy '*' --interface 2.56.119.52 \
    --resolve "archive.ubuntu.com:80:$dst" --connect-timeout 5 --max-time 12 -v \
    --range 0-1023 -D - -o /dev/null \
    'http://archive.ubuntu.com/ubuntu/pool/universe/d/docker-compose-v2/docker-compose-v2_2.40.3%2bds1-0ubuntu1_amd64.deb'
done
ip -4 route show default
```

Первый адрес зеркала отдал `206`, второй установил TCP, но не отдал HTTP-ответ. Временный маршрут откатился.

### Постоянное исправление Netplan (история)

Перед изменениями общего хоста — согласование с владельцем, консоль провайдера и проверка Endpoint VPN. Исторически перестановка IPv4 не помогла и не была подтверждена. Затем из исходного YAML удалили только старый IPv4, не меняя IPv6, DNS и шлюз.

```bash
cat /etc/netplan/10-net0.yaml
cp -a /etc/netplan/10-net0.yaml /root/10-net0.yaml.before-zvonilka
nano /etc/netplan/10-net0.yaml       # первая попытка: перестановка IPv4
diff -u /root/10-net0.yaml.before-zvonilka /etc/netplan/10-net0.yaml
netplan try --timeout 180            # не подтверждать неудачную перестановку
```

Проверка во второй сессии в каждом прогоне:

```bash
ip -br -4 addr show dev net0
ip -4 route get 8.8.8.8
timeout 10s getent ahostsv4 archive.ubuntu.com
```

После отката первой попытки:

```bash
cp -a /root/10-net0.yaml.before-zvonilka /etc/netplan/10-net0.yaml
nano /etc/netplan/10-net0.yaml       # удалить только 176.124.207.191/32
diff -u /root/10-net0.yaml.before-zvonilka /etc/netplan/10-net0.yaml
netplan try --timeout 180
```

Повторить проверки из новой SSH-сессии. Рабочий результат: `src 2.56.119.52` и DNS отвечает. Для подтверждения также проверить реальное подключение клиента Amnezia; затем Enter в `netplan try`, без отдельного `netplan apply`.

### Успешная установка Compose

```bash
apt-get install --no-install-recommends docker-compose-v2
docker compose version
systemctl show docker.service -p MainPID -p ExecMainStartTimestamp
docker inspect --format 'VPN: {{.State.Status}}, started={{.State.StartedAt}}, restarts={{.RestartCount}}' amnezia-awg2
```

Compose `2.40.3+ds1-0ubuntu1`, PID Docker `2357` и время запуска контейнера VPN не изменились.

## 4. Сборка на Mint, загрузка готового образа

Из корня проекта. Если override уже добавлен, повторно патч не применять.

```bash
git apply --check zvonilka-vps-prebuilt.patch && git apply zvonilka-vps-prebuilt.patch
VPS=root@2.56.119.52
REV="$(git rev-parse HEAD)"
IMAGE="zvonilka-backend:$REV"
docker build --platform linux/amd64 -f zvonilka/backend/Dockerfile -t "$IMAGE" .
ssh "$VPS" 'whoami'
set -o pipefail
docker image save "$IMAGE" | gzip -1 | ssh "$VPS" 'docker image load'
ssh "$VPS" 'install -d -m 0750 /opt/zvonilka-staging'
printf 'ZVONILKA_STAGING_IMAGE=%s\n' "$IMAGE" > release.env
scp compose.staging.yaml compose.staging.vps.yaml release.env "$VPS:/opt/zvonilka-staging/"
```

если до scp не дошли, только образ если отправлися

```bash
printf '%s\n' \
  'ZVONILKA_STAGING_IMAGE=zvonilka-backend:3e60ca03b5e6c05d4791cf1ae7f71a600c3b10f7' \
  > release.env

scp compose.staging.yaml compose.staging.vps.yaml release.env \
  root@2.56.119.52:/opt/zvonilka-staging/

```

При первом подключении сверить отпечаток через доверенную сессию на VPS: `ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`. Пароль при вводе не отображается, передача образа может долго идти без вывода. Успех: `Loaded image: zvonilka-backend:b9cc369e6ac8cc6d558a3243f7a640f9f071a096`. Образ хранится в Docker, а не в рабочем каталоге. Override снимает `build` у `migrate` и `api` через `!reset null`.

## 5. Backend на VPS

```bash
cd /opt/zvonilka-staging
ls -la
docker image inspect --format '{{.Os}}/{{.Architecture}} {{.Id}}' \
  zvonilka-backend:b9cc369e6ac8cc6d558a3243f7a640f9f071a096
dc() {
  docker compose -p zvonilka-staging \
    --env-file shared/compose.env --env-file release.env \
    -f compose.staging.yaml -f compose.staging.vps.yaml "$@"
}
install -d -m 0700 shared
(umask 077; nano shared/compose.env)
chmod 0600 shared/compose.env
ls shared/
dc config --quiet && dc config --images
dc up -d --no-build
dc ps -a
curl --max-time 5 -fsS http://127.0.0.1:8002/health/ready
```

`shared/compose.env` — обычный файл Compose, не Ansible Vault. Нужны `ZVONILKA_STAGING_DB_PASSWORD` и `ZVONILKA_STAGING_JWT_SECRET`; не коммитить и не печатать полный `dc config`. Без файла ошибка `couldn't find env file`. При повторном деплое не заменять пароль уже инициализированной PostgreSQL простой правкой env. Итог: `migrate Exited (0)`, API/PostgreSQL/Redis healthy, loopback вернул `{"status":"ready"}`.

## 6. Упрощённый Ansible: Nginx и сертификат

На Mint. Домен `api.zvonilka.website` разрешался через wildcard A в `2.56.119.52`; AAAA не публиковали. Адрес IPv6 из Netplan — `2a0b:4140:e305::2`, но доступность входящего IPv6 не проверена. `ping` корневого домена показал IPv4, но без ответов; входящие echo-request отбрасывались, это не проверка TCP.

```bash
ping zvonilka.website    # остановить Ctrl+C
dig +short A api.zvonilka.website
dig +short AAAA api.zvonilka.website
cd infra/ansible
```

Рядом с `edge-zvonilka.yml` файл `edge-vps.ini`:

```ini
[zvonilka_edge]
swiss-vps ansible_host=2.56.119.52 ansible_user=root
```

Первый прогон готовит HTTP, второй выдаёт сертификат. Плейбук не управляет Docker, VPN, SSH, сетевыми настройками или firewall.

```bash
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --syntax-check
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --ask-pass
curl -4 --max-time 10 -fsS \
  http://api.zvonilka.website/.well-known/acme-challenge/zvonilka-probe
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --ask-pass \
  -e '{"edge_issue":true,"edge_email":"you@example.com"}'
```

Ожидание для HTTP-пробы — `zvonilka-acme-ok`. Заменить пример email на рабочий. HTTP-vhost доступен снаружи по 80/tcp; после выпуска добавляется HTTPS на 443/tcp. Регистрация через оба публичных пути закрыта; сохранены точный маршрут `/api/v1/rooms` и WS Upgrade.

### История сбоев Certbot (диагностика на VPS)

```bash
grep -E 'Sending (GET|HEAD|POST) request|Starting new HTTPS|ConnectionResetError|urn:ietf:params:acme:error' \
  /var/log/letsencrypt/letsencrypt.log | tail -n 25
ip -4 route get 8.8.8.8
ACME='https://acme-v02.api.letsencrypt.org/directory'
for family in 4 6; do
  curl "-$family" --noproxy '*' --connect-timeout 5 --max-time 15 -v \
    -o /dev/null -w '\nHTTP=%{http_code} remote=%{remote_ip}\n' "$ACME"
done
/usr/bin/python3 -u - <<'PYACME'
import sys
import requests

def audit(event, args):
    if event == 'socket.connect':
        print('connect:', args[1], flush=True)

sys.addaudithook(audit)
try:
    r = requests.get('https://acme-v02.api.letsencrypt.org/directory', timeout=(5, 10))
    print('HTTP:', r.status_code)
    r.raise_for_status()
except requests.RequestException as exc:
    print(type(exc).__name__, exc)
    sys.exit(1)
PYACME
curl -4 --http1.1 --noproxy '*' --connect-timeout 5 --max-time 15 \
  -fsS -o /dev/null -w 'HTTP=%{http_code} remote=%{remote_ip}\n' "$ACME"
grep -E 'Sending (GET|HEAD|POST) request|Received response|urn:ietf:params:acme:error|ReadTimeout' \
  /var/log/letsencrypt/letsencrypt.log | tail -n 35
grep -A 35 -F '2026-11-22 13:25:03,811:DEBUG:acme.client:Received response:' \
  /var/log/letsencrypt/letsencrypt.log
certbot certificates --cert-name api.zvonilka.website
test -s /etc/letsencrypt/live/api.zvonilka.website/fullchain.pem &&
  test -s /etc/letsencrypt/live/api.zvonilka.website/privkey.pem &&
  echo 'Сертификат и ключ на месте'
```

Первая попытка: reset на `/directory`. Вторая: CA вернул `valid` на `/finalize`, затем `/order` завис и локального сертификата не было. Третья: тот же Ansible-прогон успешен, `nginx -t` и reload прошли. IPv6 не отключали, Nginx/DNS из-за сбоев не переделывали. Не повторять выпуск в цикле. Проверка `curl -4 --http1.1` с Mint тоже вернула 200; `remote_ip` — IP назначения, не адрес клиента.

### Финальная приёмка Ansible и TLS

На Mint:

```bash
curl -4 -fsS https://api.zvonilka.website/health/ready
ansible-playbook -i edge-vps.ini edge-zvonilka.yml --ask-pass \
  -e '{"edge_issue":true,"edge_email":"you@example.com"}'
```

Повторный прогон `ok=11 changed=0 failed=0`. На VPS:

```bash
certbot renew --dry-run
systemctl list-timers --all certbot.timer
```

Тест продления успешен, таймер запланирован.

## 7. Приёмка клиентом (после Ansible)

На Mint из корня репозитория:

```bash
cd zvonilka/desktop
npm run dev
```

Профиль `Staging`, базовый URL `https://api.zvonilka.website`. Два тестовых пользователя созданы на VPS через loopback, не через Nginx. Пример команды (повторить для второго пользователя; реальные значения не включены):

```bash
curl -X POST 'http://127.0.0.1:8002/api/v1/auth/register' \
  -H 'accept: */*' -H 'Content-Type: application/json' \
  --data-binary @-
```

Подать через stdin JSON с `username`, `display_name` и `password`, затем EOF (`Ctrl+D`). Для сверки отчётов с сервером на VPS (функция `dc` из раздела 5):

```bash
dc logs --no-color api | grep -F '90f4af87-7318-41fa-a49d-a8f0758ddda9'
dc logs --no-color api | grep -F '38899c6a-a4c6-4a05-a942-4b6fada8a2f8'
```

Подтверждены `ws_joined`, heartbeat у обоих клиентов, штатный выход и RTP в обе стороны по прямому `host → host / UDP`. TURN/STUN, звонок из разных сетей и установщик для друга — следующие задачи, не часть выполненного деплоя.

# Coturn test

```bash
ansible-vault create turn-vault.yml
vault_zvonilka_turn_secret: "..." - openssl rand -hex 32


ansible-playbook -i edge-vps.ini turn-zvonilka.yml --syntax-check

ansible-playbook -i edge-vps.ini turn-zvonilka.yml \
  --limit swiss-vps --ask-pass --ask-vault-pass \
  -e @turn-vault.yml

# на сервере

systemctl is-active coturn
ss -lntup '( sport = :3478 or sport = :5349 )'
journalctl -u coturn -n 40 --no-pager

docker inspect --format \
  'VPN: {{.State.Status}}, started={{.State.StartedAt}}, restarts={{.RestartCount}}' \
  amnezia-awg2
```

```bash
sudo apt update
sudo apt install coturn -y

# на пк
timeout 10s turnutils_stunclient -p 3478 2.56.119.52
```

на впс создаем временного юзера coturn

```bash
python3 - <<'PY'
import base64
import hmac
import time
from pathlib import Path

prefix = "static-auth-secret="
secrets = [
    line.strip()[len(prefix):]
    for line in Path("/etc/turnserver.conf").read_text().splitlines()
    if line.strip().startswith(prefix)
]
if len(secrets) != 1 or not secrets[0]:
    raise SystemExit("Ожидался один static-auth-secret в конфиге coturn")

username = f"{int(time.time()) + 600}:zvonilka-smoke"
password = base64.b64encode(
    hmac.digest(secrets[0].encode(), username.encode(), "sha1")
).decode()
print("Username:", username)
print("Password:", password)
PY
```

local pc

```bash
read -rp 'TURN username: ' TURN_USER
read -rsp 'TURN password: ' TURN_PASS
printf '\n'

# Mint → coturn по UDP
timeout 20s turnutils_uclient -y -c -n 5 \
  -u "$TURN_USER" -w "$TURN_PASS" -p 3478 2.56.119.52

# Mint → coturn по TCP; само реле по-прежнему использует UDP
timeout 20s turnutils_uclient -t -y -c -n 5 \
  -u "$TURN_USER" -w "$TURN_PASS" -p 3478 2.56.119.52

unset TURN_USER TURN_PASS
```
