#!/usr/bin/env bash
#
# Деплой бота на VPS. Запускается вручную из корня проекта (или по полному пути).
# Полный порядок действий, включая откат, описан в DEPLOY.md.
#
# Что делает:
#   1. git pull --ff-only
#   2. собирает образ bot:<короткий git sha> (compose.yaml, сервис bot)
#   3. сравнивает ревизию Alembic в новом образе с ревизией в БД и предупреждает, если при старте
#      применится миграция
#   4. делает бэкап БД через scripts/backup_db.sh, пока старый бот ещё работает
#      (если бэкап не удался, деплой прерывается и старый бот остаётся работать)
#   5. переставляет bot:current на новый образ и пересоздаёт контейнер bot без пересборки
#   6. в несколько попыток проверяет, что бот поднялся (иначе подсказывает команду отката)
#   7. пишет строку в .deploy_history
#
# Старые образы bot:<sha> при этом НЕ удаляются: они нужны для мгновенного отката
# (scripts/rollback.sh). Чистить их — отдельной командой, когда сами решите:
#   ./scripts/deploy.sh --prune-images [N]     # оставить N последних (по умолчанию 5) + текущий
#
# СХЕМА БД: миграции Alembic применяет сам бот при старте нового образа (run_migrations в run.py).
# Откат образа (rollback.sh) схему назад НЕ откатывает — это отдельное действие, см. DEPLOY.md.
#
# Использование:
#   ./scripts/deploy.sh [--no-backup] [--yes]
#   ./scripts/deploy.sh --prune-images [N] [--yes]
#
#   --no-backup   не делать бэкап перед деплоем. Игнорируется, если деплой применит миграцию.
#   --yes         не задавать вопросов (повторный деплой откаченного коммита, удаление образов)

set -Eeuo pipefail

# shellcheck source=scripts/deploy_common.sh
source "$(dirname "${BASH_SOURCE[0]}")/deploy_common.sh"

usage() {
    sed -n '/^# Использование:/,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2
    exit 2
}

prune_images() {
    local keep="$1" assume_yes="$2"
    [[ "$keep" =~ ^[0-9]+$ ]] || fail "--prune-images: N должно быть целым числом, получено '$keep'"

    local current_id
    current_id="$(image_id "$CURRENT_TAG")"

    # docker image ls выдаёт образы от новых к старым
    local -a candidates=()
    local tag id kept=0
    while read -r tag id; do
        [[ "$tag" == "$CURRENT_TAG" || "$tag" == "<none>" ]] && continue
        if [[ -n "$current_id" && "$id" == "$current_id" ]]; then
            continue  # задеплоенный сейчас образ не удаляем никогда
        fi
        if (( kept < keep )); then
            kept=$((kept + 1))
            continue
        fi
        candidates+=("$tag")
    done < <(docker image ls --no-trunc --format '{{.Tag}} {{.ID}}' "$BOT_IMAGE")

    if (( ${#candidates[@]} == 0 )); then
        log "Удалять нечего: образов $BOT_IMAGE сверх $keep последних (не считая текущего) нет"
        return 0
    fi

    log "Будут удалены образы (текущий и $keep последних остаются):"
    for tag in "${candidates[@]}"; do
        echo "  $BOT_IMAGE:$tag"
    done
    if [[ "$assume_yes" != "true" ]]; then
        confirm "Удалить эти образы? Откат на них станет невозможен" || fail "отменено"
    fi

    for tag in "${candidates[@]}"; do
        docker rmi "$BOT_IMAGE:$tag" || warn "не удалось удалить $BOT_IMAGE:$tag"
    done
    log "Очистка завершена. Слои без тегов можно убрать: docker image prune"
}

# Последний откат в .deploy_history: с какого тега откатывались ("предыдущий тег" в строке rollback).
last_rolled_back_from_tag() {
    [[ -f "$DEPLOY_HISTORY" ]] || return 0
    awk -F'\t' '$2 == "deploy" && $6 == "ok" { from = "" } $2 == "rollback" && $6 == "ok" { from = $4 } END { print from }' \
        "$DEPLOY_HISTORY"
}

deploy() {
    local no_backup="$1" assume_yes="$2"

    require_docker_compose

    if ! git diff --quiet HEAD; then
        git status --short >&2
        fail "в рабочем дереве есть незакоммиченные изменения — деплой собирал бы не то, что в git"
    fi

    log "git pull --ff-only"
    git pull --ff-only || fail "git pull не удался (расхождение с origin? разберитесь вручную)"

    local tag
    tag="$(git rev-parse --short HEAD)"
    log "Коммит: $(git log -1 --format='%h %s')"

    # Защита от повторного деплоя того же сломанного коммита после отката (см. DEPLOY.md, «Откат»):
    # rollback.sh не трогает git, поэтому без git revert pull ничего не изменит.
    local rolled_back_from
    rolled_back_from="$(last_rolled_back_from_tag)"
    if [[ -n "$rolled_back_from" && "$rolled_back_from" == "$tag" ]]; then
        warn "последний откат был именно С коммита $tag, а HEAD по-прежнему на нём."
        warn "Скорее всего, забыт 'git revert' проблемного коммита (DEPLOY.md, раздел «Откат»)."
        if [[ "$assume_yes" != "true" ]]; then
            confirm "Всё равно задеплоить $tag повторно?" || fail "отменено"
        fi
    fi

    local previous_tag
    previous_tag="$(current_deployed_tag)"

    log "Сборка образа $BOT_IMAGE:$tag"
    BOT_TAG="$tag" docker compose build --pull bot

    local image_head db_revision migration=false
    image_head="$(image_alembic_head "$tag")"
    db_revision="$(db_alembic_revision)"

    if [[ -z "$db_revision" ]]; then
        warn "в БД нет alembic_version. Если это существующая прод-база, бот откажется стартовать"
        warn "(UnstampedDatabaseError) — сначала разметка по MIGRATIONS.md §2. Пустая база создастся сама."
        migration=true
    elif [[ "$db_revision" != "$image_head" ]]; then
        warn "при старте применится миграция схемы: $db_revision -> $image_head"
        warn "Откат образа после этого НЕ вернёт схему назад (DEPLOY.md, «Откат»)."
        migration=true
    else
        log "Схема БД уже на ревизии образа ($image_head), миграций не будет"
    fi

    if [[ "$no_backup" == "true" && "$migration" == "true" ]]; then
        warn "--no-backup проигнорирован: перед изменением схемы бэкап обязателен"
        no_backup=false
    fi

    if [[ "$no_backup" == "true" ]]; then
        warn "бэкап перед деплоем пропущен (--no-backup)"
    else
        log "Бэкап БД перед деплоем (старый бот пока работает)"
        BACKUP_LABEL="predeploy-$tag" "$PROJECT_DIR/scripts/backup_db.sh" \
            || fail "бэкап не удался — деплой прерван, работает прежний образ ${previous_tag:-(неизвестен)}"
    fi

    switch_current_and_restart "$tag"

    if wait_bot_healthy; then
        record_history deploy "$tag" "$previous_tag" "$image_head" ok
        log "Деплой $tag завершён (предыдущий образ: ${previous_tag:-нет})"
        if [[ -n "$previous_tag" && "$previous_tag" != "$tag" ]]; then
            log "Откат при необходимости: ./scripts/rollback.sh $previous_tag"
        fi
        return 0
    fi

    show_bot_logs_tail
    record_history deploy "$tag" "$previous_tag" "$image_head" failed
    log "ДЕПЛОЙ $tag НЕ ПОДНЯЛСЯ." >&2
    if [[ -n "$previous_tag" && "$previous_tag" != "$tag" ]]; then
        log "Откат на предыдущий образ: ./scripts/rollback.sh $previous_tag" >&2
        if [[ "$migration" == "true" ]]; then
            log "Была миграция схемы — rollback.sh сначала попросит вернуть схему (DEPLOY.md, «Откат»)." >&2
        fi
        log "После отката: git revert проблемного коммита, и только потом снова deploy.sh." >&2
    fi
    exit 1
}

main() {
    cd "$PROJECT_DIR"

    local no_backup=false assume_yes=false prune=false keep=5
    while (( $# > 0 )); do
        case "$1" in
            --no-backup) no_backup=true ;;
            --yes|-y) assume_yes=true ;;
            --prune-images)
                prune=true
                if [[ $# -gt 1 && "$2" != --* ]]; then
                    keep="$2"
                    shift
                fi
                ;;
            -h|--help) usage ;;
            *) log "неизвестный аргумент: $1" >&2; usage ;;
        esac
        shift
    done

    if [[ "$prune" == "true" ]]; then
        prune_images "$keep" "$assume_yes"
    else
        deploy "$no_backup" "$assume_yes"
    fi
}

# Весь скрипт разбирается bash целиком до вызова main — поэтому git pull, обновивший сам deploy.sh,
# не сломает уже выполняющийся экземпляр.
main "$@"
