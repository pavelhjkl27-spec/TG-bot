#!/usr/bin/env bash
#
# Общие функции для scripts/deploy.sh и scripts/rollback.sh. Сам по себе не запускается,
# подключается через `source`.
#
# Схема тегов образа бота (см. `image:` сервиса bot в compose.yaml):
#   bot:<git sha>  — неизменяемый образ конкретного коммита, по одному на каждый деплой;
#   bot:current    — «указатель» на образ, который сейчас задеплоен. compose.yaml по умолчанию
#                    поднимает именно его, поэтому обычный `docker compose up -d` или перезагрузка
#                    VPS запускают задеплоенный образ, а не то, что лежит в HEAD.
#
# Переменные окружения (все необязательные):
#   BOT_IMAGE               имя образа (по умолчанию "bot"), должно совпадать с compose.yaml
#   DEPLOY_CHECK_ATTEMPTS   сколько раз проверять, что бот поднялся (по умолчанию 12)
#   DEPLOY_CHECK_INTERVAL   пауза между проверками в секундах (по умолчанию 5)

# cron / ssh без login-shell могут дать урезанный PATH — docker может не найтись без этого
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:${PATH:-}"

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export BOT_IMAGE="${BOT_IMAGE:-bot}"
CURRENT_TAG="current"
DEPLOY_HISTORY="$PROJECT_DIR/.deploy_history"
DEPLOY_CHECK_ATTEMPTS="${DEPLOY_CHECK_ATTEMPTS:-12}"
DEPLOY_CHECK_INTERVAL="${DEPLOY_CHECK_INTERVAL:-5}"

# Строка, которую run.py пишет в лог после миграций и успешного getMe, прямо перед polling.
# Она подтверждает, что схема применена, а токен и связь с Telegram рабочие.
# Если меняете её в run.py — поменяйте и здесь.
BOT_READY_MARKER="Bot is ready"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }
warn() { log "ВНИМАНИЕ: $*" >&2; }
fail() { log "ERROR: $*" >&2; exit 1; }

require_docker_compose() {
    command -v docker >/dev/null 2>&1 || fail "docker не установлен или не в PATH"
    docker compose version >/dev/null 2>&1 || fail "нет плагина docker compose (нужен 'docker compose', не 'docker-compose')"
    [[ -f "$PROJECT_DIR/.env" ]] || fail "нет файла .env в $PROJECT_DIR"
}

# Спросить подтверждение. Без терминала (и без --yes у вызывающего) — отказ, а не молчаливое «да».
confirm() {
    local prompt="$1"
    if [[ ! -t 0 ]]; then
        fail "нужно подтверждение ('$prompt'), но терминала нет — запустите интерактивно или с --yes"
    fi
    local answer
    read -r -p "$prompt [y/N] " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]]
}

image_exists() {
    docker image inspect "$BOT_IMAGE:$1" >/dev/null 2>&1
}

image_id() {
    docker image inspect --format '{{.Id}}' "$BOT_IMAGE:$1" 2>/dev/null || true
}

# Какой sha-тег сейчас задеплоен: ищем тег (не "current") с тем же image id, что у bot:current.
# Пустая строка — если bot:current ещё нет (первый деплой) или тег sha уже удалён.
current_deployed_tag() {
    local current_id
    current_id="$(image_id "$CURRENT_TAG")"
    [[ -n "$current_id" ]] || return 0
    docker image ls --no-trunc --format '{{.Tag}} {{.ID}}' "$BOT_IMAGE" \
        | awk -v id="$current_id" -v cur="$CURRENT_TAG" '$2 == id && $1 != cur { print $1; exit }'
}

# Ревизия Alembic, до которой миграции в образе доводят схему (`alembic heads`).
# env.py при этом не выполняется, поэтому ни .env, ни БД не нужны.
image_alembic_head() {
    local tag="$1" output heads_count
    output="$(docker run --rm --entrypoint alembic "$BOT_IMAGE:$tag" heads)" \
        || fail "не удалось выполнить 'alembic heads' в образе $BOT_IMAGE:$tag"
    heads_count="$(grep -c . <<<"$output" || true)"
    [[ "$heads_count" == "1" ]] \
        || fail "в образе $BOT_IMAGE:$tag не ровно одна head-ревизия Alembic:"$'\n'"$output"
    awk '{ print $1 }' <<<"$output"
}

# Текущая ревизия схемы в прод-БД (содержимое alembic_version).
# Пустая строка — таблицы alembic_version нет (база не размечена, см. MIGRATIONS.md §2).
db_psql() {
    # SQL приходит через stdin — так не нужно экранировать кавычки внутри sh -c
    docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAq -v ON_ERROR_STOP=1'
}

db_alembic_revision() {
    local has_table
    docker compose up -d --wait db >/dev/null \
        || fail "не удалось запустить/дождаться сервис db"
    has_table="$(db_psql <<<"SELECT to_regclass('public.alembic_version') IS NOT NULL;")" \
        || fail "не удалось прочитать схему БД"
    [[ "$has_table" == "t" ]] || return 0
    db_psql <<<"SELECT version_num FROM alembic_version;" \
        || fail "не удалось прочитать alembic_version"
}

bot_container_id() {
    docker compose ps -q bot
}

# Проверяет, что свежепересозданный контейнер бота действительно поднялся. Несколько попыток с паузой
# (как цикл подключения к БД в run.py), а не одна фиксированная задержка: миграция при старте может
# занять дольше обычного. Успех — контейнер работает и в логе есть BOT_READY_MARKER. Если контейнер
# упал или уже перезапускался (restart: unless-stopped), это провал сразу, без ожидания остальных попыток.
wait_bot_healthy() {
    local attempt cid status restarts

    for (( attempt = 1; attempt <= DEPLOY_CHECK_ATTEMPTS; attempt++ )); do
        cid="$(bot_container_id)"
        if [[ -z "$cid" ]]; then
            warn "контейнер bot не найден"
            return 1
        fi

        status="$(docker inspect --format '{{.State.Status}}' "$cid")"
        restarts="$(docker inspect --format '{{.RestartCount}}' "$cid")"

        if [[ "$status" == "exited" || "$status" == "dead" || "$restarts" -gt 0 ]]; then
            warn "бот упал при старте (status=$status, перезапусков: $restarts)"
            return 1
        fi

        if [[ "$status" == "running" ]] && docker logs "$cid" 2>&1 | grep -qF "$BOT_READY_MARKER"; then
            log "Бот поднялся (попытка $attempt/$DEPLOY_CHECK_ATTEMPTS): в логе есть '$BOT_READY_MARKER'"
            return 0
        fi

        if (( attempt < DEPLOY_CHECK_ATTEMPTS )); then
            log "Бот ещё не готов (попытка $attempt/$DEPLOY_CHECK_ATTEMPTS, status=$status), жду ${DEPLOY_CHECK_INTERVAL}с"
            sleep "$DEPLOY_CHECK_INTERVAL"
        fi
    done

    warn "за $DEPLOY_CHECK_ATTEMPTS попыток в логе так и не появилось '$BOT_READY_MARKER'"
    return 1
}

show_bot_logs_tail() {
    local cid
    cid="$(bot_container_id)"
    [[ -n "$cid" ]] || return 0
    echo "----- последние строки лога bot -----" >&2
    docker logs --tail 40 "$cid" >&2 2>&1 || true
    echo "-------------------------------------" >&2
}

# Переключить bot:current на указанный тег и пересоздать контейнер без сборки.
switch_current_and_restart() {
    local tag="$1"
    docker tag "$BOT_IMAGE:$tag" "$BOT_IMAGE:$CURRENT_TAG"
    log "$BOT_IMAGE:$CURRENT_TAG -> $BOT_IMAGE:$tag, пересоздаю контейнер bot"
    docker compose up -d --no-build --force-recreate bot
}

# Формат строки: дата<TAB>действие<TAB>новый тег<TAB>предыдущий тег<TAB>ревизия alembic<TAB>результат
record_history() {
    local action="$1" tag="$2" previous="$3" revision="$4" result="$5"
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
        "$(date '+%Y-%m-%d %H:%M:%S')" "$action" "$tag" "${previous:--}" "${revision:--}" "$result" \
        >>"$DEPLOY_HISTORY"
}
