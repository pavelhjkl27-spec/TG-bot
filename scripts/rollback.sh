#!/usr/bin/env bash
#
# Мгновенный откат бота на ранее собранный образ bot:<sha> — без git и без пересборки.
# Полный порядок действий описан в DEPLOY.md, раздел «Откат».
#
# !!! ОТКАТ ОБРАЗА НЕ ОТКАТЫВАЕТ СХЕМУ БД !!!
# Это два разных действия. Если после целевого образа деплоилась миграция Alembic, в БД лежит
# ревизия, о которой старый код не знает. Бот при старте делает `alembic upgrade head`, падает с
# "Can't locate revision" и перезапускается по кругу. Поэтому скрипт сначала сравнивает ревизию
# в целевом образе с ревизией в БД и при расхождении ОТКАЗЫВАЕТСЯ переключаться. Схему нужно сначала
# вернуть вручную (бэкап + `alembic downgrade` ТЕКУЩИМ образом, в котором есть downgrade-скрипт),
# см. DEPLOY.md. Downgrade может удалить данные (например, колонку вместе с содержимым).
#
# Git скрипт намеренно НЕ трогает: репозиторий остаётся на HEAD с проблемным коммитом.
# После отката сделайте `git revert` этого коммита, и только потом снова запускайте deploy.sh.
# Иначе deploy.sh пересоберёт и выкатит тот же сломанный коммит.
#
# Использование:
#   ./scripts/rollback.sh --list
#   ./scripts/rollback.sh <sha или тег> [--ignore-schema-check]
#
#   --list                  показать доступные образы и последние записи .deploy_history
#   --ignore-schema-check   переключиться, даже если ревизия схемы не совпадает (только если точно
#                           понимаете, что старый код с этой схемой стартует)

set -Eeuo pipefail

# shellcheck source=scripts/deploy_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/deploy_common.sh"

usage() {
    sed -n '/^# Использование:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

list_images() {
    local current_id
    current_id="$(image_id "$CURRENT_TAG")"

    log "Образы $BOT_IMAGE (от новых к старым), * — задеплоен сейчас:"
    local tag id created mark
    while IFS=$'\t' read -r tag id created; do
        [[ "$tag" == "$CURRENT_TAG" || "$tag" == "<none>" ]] && continue
        mark=" "
        [[ -n "$current_id" && "$id" == "$current_id" ]] && mark="*"
        printf '  %s %-12s %s\n' "$mark" "$tag" "$created"
    done < <(docker image ls --no-trunc --format $'{{.Tag}}\t{{.ID}}\t{{.CreatedAt}}' "$BOT_IMAGE")

    if [[ -f "$DEPLOY_HISTORY" ]]; then
        echo
        log "Последние записи .deploy_history (дата, действие, тег, предыдущий, ревизия alembic, результат):"
        tail -n 10 "$DEPLOY_HISTORY" | sed 's/^/  /'
    fi
}

rollback() {
    local target="$1" ignore_schema="$2"

    require_docker_compose

    # Принимаем и полный sha: образы тегируются коротким (git rev-parse --short)
    if ! image_exists "$target" && git cat-file -e "$target^{commit}" 2>/dev/null; then
        local short
        short="$(git rev-parse --short "$target")"
        image_exists "$short" && target="$short"
    fi
    [[ "$target" != "$CURRENT_TAG" ]] || fail "укажите конкретный sha-тег, а не '$CURRENT_TAG'"
    image_exists "$target" \
        || fail "образа $BOT_IMAGE:$target нет локально (пересборку rollback.sh не делает). Доступные: ./scripts/rollback.sh --list"

    local previous_tag
    previous_tag="$(current_deployed_tag)"
    if [[ -n "$previous_tag" && "$previous_tag" == "$target" ]]; then
        log "$BOT_IMAGE:$target уже задеплоен — пересоздаю контейнер на нём же"
    fi

    local image_head db_revision
    image_head="$(image_alembic_head "$target")"
    db_revision="$(db_alembic_revision)"

    if [[ "$db_revision" != "$image_head" ]]; then
        if [[ "$ignore_schema" == "true" ]]; then
            warn "ревизия схемы не совпадает (БД: ${db_revision:-нет}, образ: $image_head), продолжаю из-за --ignore-schema-check"
        else
            log "ОТКАТ ОСТАНОВЛЕН: схема БД не совпадает с целевым образом." >&2
            log "  ревизия в БД:            ${db_revision:-(нет alembic_version)}" >&2
            log "  ревизия образа $target: $image_head" >&2
            log "Старый образ с такой схемой не стартует. Откат образа схему не откатывает — это отдельный шаг:" >&2
            log "  1) docker compose stop bot" >&2
            log "  2) BACKUP_LABEL=pre-downgrade ./scripts/backup_db.sh" >&2
            log "  3) docker compose run --rm bot alembic downgrade $image_head   # ТЕКУЩИМ образом; может удалить данные" >&2
            log "  4) ./scripts/rollback.sh $target" >&2
            log "Подробно: DEPLOY.md, раздел «Откат»." >&2
            exit 1
        fi
    else
        log "Схема БД ($db_revision) совпадает с целевым образом"
    fi

    switch_current_and_restart "$target"

    if wait_bot_healthy; then
        record_history rollback "$target" "$previous_tag" "$image_head" ok
        log "Откат на $target завершён (был: ${previous_tag:-неизвестно}). Схема БД этим откатом не менялась."
        if [[ "$target" != "$(git rev-parse --short HEAD)" ]]; then
            log "НЕ ЗАБУДЬТЕ: git revert проблемного коммита (+ push), и только потом снова deploy.sh —"
            log "иначе deploy.sh пересоберёт и выкатит тот же сломанный HEAD."
        fi
        return 0
    fi

    show_bot_logs_tail
    record_history rollback "$target" "$previous_tag" "$image_head" failed
    fail "после отката на $target бот не поднялся — смотрите логи выше (docker compose logs bot)"
}

main() {
    cd "$PROJECT_DIR"

    local target="" ignore_schema=false list=false
    while (( $# > 0 )); do
        case "$1" in
            --list) list=true ;;
            --ignore-schema-check) ignore_schema=true ;;
            -h|--help) usage ;;
            --*) log "неизвестный аргумент: $1" >&2; usage ;;
            *)
                [[ -z "$target" ]] || { log "тег указан дважды: $target и $1" >&2; usage; }
                target="$1"
                ;;
        esac
        shift
    done

    if [[ "$list" == "true" ]]; then
        list_images
        return 0
    fi
    [[ -n "$target" ]] || usage
    rollback "$target" "$ignore_schema"
}

main "$@"
