#!/usr/bin/env bash
#
# Ежедневный бэкап Postgres из docker compose (сервис `db`).
#
# Что делает:
#   1. pg_dump внутри контейнера `db` -> gzip -> backups/backup-YYYY-MM-DD.sql.gz на хосте
#   2. (опционально) выгружает архив за пределы VPS через rclone
#   3. удаляет локальные архивы старше BACKUP_RETENTION_DAYS (по умолчанию 14) дней
#
# Любая ошибка (pg_dump, gzip, проверка архива, rclone) -> ненулевой код выхода.
# Старые бэкапы удаляются только после того, как новый успешно создан.
#
# Переменные окружения (все необязательные):
#   BACKUP_DIR                каталог для архивов (по умолчанию <проект>/backups)
#   BACKUP_RETENTION_DAYS     сколько дней хранить локальные архивы (по умолчанию 14)
#   BACKUP_REMOTE_ENABLED     "true" — включить выгрузку через rclone (по умолчанию выключено)
#   BACKUP_RCLONE_REMOTE      имя remote из `rclone config` (например "b2")
#   BACKUP_RCLONE_PATH        bucket/путь внутри remote (например "my-bot-backups/db")
#
# POSTGRES_USER / POSTGRES_DB на хосте НЕ нужны: pg_dump берёт их из окружения
# самого контейнера `db` (их туда уже передаёт compose.yaml из .env).

set -Eeuo pipefail

# cron запускает с урезанным PATH — docker может не найтись без этого
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${BACKUP_DIR:-$PROJECT_DIR/backups}"
BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_REMOTE_ENABLED="${BACKUP_REMOTE_ENABLED:-false}"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
trap 'log "ERROR: команда упала на строке $LINENO (код $?)" >&2' ERR

# docker compose ищет compose.yaml и .env в текущем каталоге
cd "$PROJECT_DIR"

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077  # архивы содержат персональные данные клиентов — только владелец

# Не даём двум запускам (cron + ручной) писать одновременно
exec 9>"$BACKUP_DIR/.backup.lock"
if command -v flock >/dev/null 2>&1; then
    flock -n 9 || fail "другой бэкап уже выполняется"
fi

STAMP="$(date +%F)"
FINAL_FILE="$BACKUP_DIR/backup-$STAMP.sql.gz"
TMP_FILE="$FINAL_FILE.partial"
# Если что-то упадёт до mv — не оставляем полузаписанный файл
trap 'rm -f "$TMP_FILE"' EXIT

log "Старт бэкапа -> $FINAL_FILE"

# -T: без TTY (иначе в cron exec падает / портит бинарный вывод)
# pipefail гарантирует, что ошибка pg_dump не будет замаскирована успешным gzip
docker compose exec -T db sh -c \
    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --no-privileges' \
    | gzip -9 > "$TMP_FILE" \
    || fail "pg_dump или gzip завершились с ошибкой"

gzip -t "$TMP_FILE" || fail "архив повреждён (gzip -t)"

# Пустой/обрезанный дамп — тоже ошибка: у pg_dump в конце всегда эта строка
gunzip -c "$TMP_FILE" | tail -n 5 | grep -q 'PostgreSQL database dump complete' \
    || fail "дамп не содержит маркер завершения — вероятно, обрезан"

mv -f "$TMP_FILE" "$FINAL_FILE"
log "Бэкап готов: $FINAL_FILE ($(du -h "$FINAL_FILE" | cut -f1))"

# ---------------------------------------------------------------------------
# Выгрузка за пределы VPS (rclone). ПО УМОЛЧАНИЮ ВЫКЛЮЧЕНО.
#
# Прежде чем включать (BACKUP_REMOTE_ENABLED=true), на VPS нужно:
#   1. Установить rclone:            curl https://rclone.org/install.sh | sudo bash
#   2. Создать bucket в Backblaze B2 (или другом S3-совместимом хранилище)
#      и application key с доступом ТОЛЬКО к этому bucket.
#   3. Выполнить `rclone config` ОТ ТОГО ЖЕ ПОЛЬЗОВАТЕЛЯ, под которым работает cron,
#      и создать remote (например с именем "b2", тип "b2" или "s3").
#   4. Проверить вручную:            rclone lsd b2:
#   5. Задать BACKUP_RCLONE_REMOTE (имя remote) и BACKUP_RCLONE_PATH (bucket/путь).
#   6. Настроить в самом bucket lifecycle rule на удаление старых файлов
#      (скрипт чистит только локальный каталог, удалённые копии не трогает).
# ---------------------------------------------------------------------------
if [[ "$BACKUP_REMOTE_ENABLED" == "true" ]]; then
    [[ -n "${BACKUP_RCLONE_REMOTE:-}" ]] || fail "BACKUP_REMOTE_ENABLED=true, но BACKUP_RCLONE_REMOTE не задан"
    [[ -n "${BACKUP_RCLONE_PATH:-}" ]] || fail "BACKUP_REMOTE_ENABLED=true, но BACKUP_RCLONE_PATH не задан"
    command -v rclone >/dev/null 2>&1 || fail "rclone не установлен"

    DEST="$BACKUP_RCLONE_REMOTE:$BACKUP_RCLONE_PATH"
    log "Выгрузка в $DEST"
    rclone copy "$FINAL_FILE" "$DEST" --checksum --retries 3 \
        || fail "rclone не смог выгрузить архив (локальная копия сохранена)"
    log "Выгрузка завершена"
else
    log "Выгрузка за пределы VPS выключена (BACKUP_REMOTE_ENABLED != true)"
fi

# Ротация — только после успешного бэкапа, только наши файлы
log "Удаляю локальные бэкапы старше $BACKUP_RETENTION_DAYS дней"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'backup-*.sql.gz' \
    -mtime +"$BACKUP_RETENTION_DAYS" -print -delete

log "Готово"
