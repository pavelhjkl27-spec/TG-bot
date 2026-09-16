# Бэкапы БД и восстановление

Скрипт: `scripts/backup_db.sh`. Архивы: `backups/backup-YYYY-MM-DD.sql.gz` — ежедневный, и
`backups/backup-YYYY-MM-DD-HHMMSS-<метка>.sql.gz` — бэкапы перед деплоем (`predeploy-<sha>`, делает
`scripts/deploy.sh`, см. `DEPLOY.md`) и ручные с `BACKUP_LABEL=...`. Все лежат на хосте, вне контейнера,
в git и в Docker-образ не попадают. Хранятся 14 дней. Для восстановления подходит любой из них.

> Во всех командах ниже `/opt/PythonProject` — путь к проекту на VPS. **Если у вас он другой,
> замените его один раз в первой команде `cd`** — дальше везде используются относительные пути.

---

## ⚠️ Прежде всего

- **Никогда не запускайте `docker compose down -v`** — флаг `-v` удаляет volume с базой.
  Обычный `docker compose down` / `stop` / `restart` данные не трогает.
- Всё, что клиенты прислали **после** времени последнего бэкапа, при восстановлении пропадёт из БД.
  Сами сообщения остаются в темах форум-группы в Telegram — оттуда их можно восстановить вручную.

---

## 🚑 Восстановление из бэкапа

Копируйте команды по порядку. Все выполняются на VPS.

### 1. Перейти в проект и выбрать архив

```bash
cd /opt/PythonProject
ls -lh backups/
```

Подставьте нужную дату (обычно — самый свежий файл):

```bash
BACKUP=backups/backup-2026-09-15.sql.gz
gzip -t "$BACKUP" && echo "АРХИВ ЦЕЛ"
```

Должно напечататься `АРХИВ ЦЕЛ`. Если нет — берите архив за предыдущий день.

**Если локальных архивов нет** (умер диск / новый сервер) и настроена выгрузка через rclone —
скачайте архив из хранилища (подставьте свои remote и bucket):

```bash
mkdir -p backups
rclone ls b2:my-bot-backups/db
rclone copy b2:my-bot-backups/db/backup-2026-09-15.sql.gz backups/
BACKUP=backups/backup-2026-09-15.sql.gz
gzip -t "$BACKUP" && echo "АРХИВ ЦЕЛ"
```

### 2. Остановить бота

```bash
docker compose stop bot
```

### 3. Убедиться, что БД запущена

```bash
docker compose up -d --wait db
```

> Новый сервер / volume потерян: `.env` должен содержать **те же** `POSTGRES_DB`, `POSTGRES_USER`,
> `POSTGRES_PASSWORD`, что и раньше. Эта команда создаст пустую БД — это нормально, переходите дальше.

### 4. Страховочная копия текущего состояния

На случай, если восстановление сделает хуже. Если БД мертва и команда падает — пропустите шаг.

```bash
docker compose exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | gzip > "backups/before-restore-$(date +%F-%H%M%S).sql.gz"
```

(Эти файлы не удаляются автоматически — удалите вручную, когда всё будет в порядке.)

### 5. Пересоздать пустую базу

```bash
docker compose exec -T db sh -c 'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
```

### 6. Залить бэкап

```bash
gunzip -c "$BACKUP" | docker compose exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -q -U "$POSTGRES_USER" -d "$POSTGRES_DB"' && echo "ВОССТАНОВЛЕНО"
```

Должно закончиться словом `ВОССТАНОВЛЕНО` (строки `set_config` / `setval` в выводе — это нормально).
Если вместо этого `ERROR:` — повторите шаги 5–6 с архивом за предыдущий день.

### 7. Проверить данные

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM requests) AS requests, (SELECT group_id FROM settings WHERE id = 1) AS group_id;"'
```

Числа должны быть похожи на ожидаемые, `group_id` — не пустой (иначе бот не привязан к группе).

### 8. Запустить бота

```bash
docker compose up -d bot
docker compose logs --tail=50 bot
```

Проверьте в Telegram: `/start` от клиента отвечает, ответ админа в теме клиента доходит.

---

## Ручной запуск бэкапа (проверка)

```bash
cd /opt/PythonProject
scripts/backup_db.sh; echo "exit=$?"
ls -lh backups/
```

`exit=0` — успех. Любое другое число — ошибка, причина в выводе строкой `ERROR:`.

## Выгрузка за пределы VPS (rclone) — пока выключена

Без неё бэкапы лежат на том же диске, что и база: отказ диска или потеря VPS уничтожат и то, и
другое. Чтобы включить:

1. Установить rclone: `curl https://rclone.org/install.sh | sudo bash`
2. Создать bucket в Backblaze B2 (или другом S3-совместимом хранилище) и ключ доступа **только к этому
   bucket**. В настройках bucket включить lifecycle rule на удаление старых файлов (например, через
   30–90 дней) — скрипт удалённые копии не чистит.
3. От пользователя, под которым работает cron: `rclone config` → создать remote (например `b2`).
4. `chmod 600 ~/.config/rclone/rclone.conf` (там лежит ключ).
5. Проверить: `rclone lsd b2:`
6. Проверить скрипт вручную:
   ```bash
   BACKUP_REMOTE_ENABLED=true BACKUP_RCLONE_REMOTE=b2 BACKUP_RCLONE_PATH=my-bot-backups/db scripts/backup_db.sh; echo "exit=$?"
   rclone ls b2:my-bot-backups/db
   ```
7. Добавить эти три переменные в строку crontab (см. ниже).

---

## Установка ежедневного запуска (cron)

Пользователь, под которым ставится cron, должен иметь доступ к Docker (`docker ps` работает без
`sudo`) и к файлу `.env`.

```bash
cd /opt/PythonProject
chmod +x scripts/backup_db.sh
mkdir -p backups && chmod 700 backups
crontab -e
```

Добавить строку (каждый день в 03:15 по времени сервера):

```cron
15 3 * * * /opt/PythonProject/scripts/backup_db.sh >> /opt/PythonProject/backups/backup.log 2>&1
```

С включённой выгрузкой через rclone — вместо неё:

```cron
15 3 * * * BACKUP_REMOTE_ENABLED=true BACKUP_RCLONE_REMOTE=b2 BACKUP_RCLONE_PATH=my-bot-backups/db /opt/PythonProject/scripts/backup_db.sh >> /opt/PythonProject/backups/backup.log 2>&1
```

Проверить на следующий день:

```bash
tail -n 20 /opt/PythonProject/backups/backup.log
ls -lh /opt/PythonProject/backups/
```
