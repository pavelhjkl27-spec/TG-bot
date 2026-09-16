# Чек-лист по секретам (выполнить вручную на VPS)

> `/opt/PythonProject` — путь к проекту на VPS; замените, если у вас другой.
> `deploy` — пользователь, под которым запускается `docker compose` и cron; замените на своего
> (узнать: выполнить `whoami` под этим пользователем).

## 1. Права на `.env`

```bash
cd /opt/PythonProject
ls -l .env
```

Установить владельца и права «читать/писать может только владелец»:

```bash
sudo chown deploy:deploy .env
chmod 600 .env
ls -l .env
```

Ожидаемый результат: `-rw------- 1 deploy deploy ... .env`

То же для каталога бэкапов (в архивах — данные клиентов):

```bash
sudo chown -R deploy:deploy backups
chmod 700 backups
chmod 600 backups/*.sql.gz
```

Если настроен rclone — там лежит ключ от хранилища:

```bash
chmod 600 ~/.config/rclone/rclone.conf
```

## 2. SSH-доступ к серверу

Проверьте (конкретные команды зависят от текущей настройки сервера):

- вход по паролю отключён, только по ключам;
- вход под `root` по SSH запрещён;
- в `~/.ssh/authorized_keys` нет чужих/забытых ключей;
- доступ к серверу есть только у тех, у кого он должен быть.

## 3. Подозрение на утечку `BOT_TOKEN`

Если токен мог утечь (попал в чат, скриншот, git, чужой компьютер) — перевыпустить сразу:

1. В Telegram: [@BotFather](https://t.me/BotFather) → `/mybots` → выбрать бота → **API Token** →
   **Revoke current token**. Старый токен перестаёт работать мгновенно.
2. Вставить новый токен в `.env` на VPS (`BOT_TOKEN=...`).
3. Перезапустить бота так, чтобы он перечитал `.env` (`restart` для этого **не подходит**):
   ```bash
   cd /opt/PythonProject
   docker compose up -d bot
   docker compose logs --tail=30 bot
   ```
