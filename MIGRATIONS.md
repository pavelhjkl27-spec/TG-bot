# Миграции БД (Alembic)

Схему БД создаёт и меняет **только Alembic**. `Base.metadata.create_all` больше не вызывается.

- Конфиг: `alembic.ini`. Окружение: `migrations/env.py` использует движок и метаданные из
  `app/database.py`, то есть URL берётся из `DATABASE_URL` (`.env`, в Docker — `compose.yaml`).
  Отдельно URL нигде не задаётся.
- Миграции: `migrations/versions/`. Первая, `22fe4fd8bb35_baseline`, описывает схему на момент перехода.
- Бот при старте сам выполняет `alembic upgrade head` (`run_migrations()` в `app/database.py`,
  до 5 попыток, как раньше с `init_db`).
- **Защита:** если в базе таблицы уже есть, а `alembic_version` нет (база создана до Alembic), бот
  **сразу** падает с `UnstampedDatabaseError`, без повторов и без попыток что-то создать. Такую базу
  один раз размечают вручную (см. ниже).
- CLI `alembic` импортирует `config.py`, поэтому ему нужны те же переменные окружения, что и боту
  (`DATABASE_URL`, `ADMIN_ID`). Локально их даёт `.env`, в контейнере — `env_file`.

> Путь к проекту на VPS ниже — `/opt/PythonProject`. Если у вас он другой, замените в первой команде `cd`.

---

## 1. Локальная dev-база

Все команды — из корня проекта с активированным `.venv`.

**Пустая база** (новая или пересозданная): ничего делать не нужно, достаточно запустить бота.

```bash
python run.py            # сам выполнит upgrade head
# или без бота:
alembic upgrade head
alembic current          # -> 22fe4fd8bb35 (head)
```

**Старая dev-база, созданная через `create_all`**: бот откажется стартовать с `UnstampedDatabaseError`.
Если в dev-базе нет ничего ценного, проще всего её пересоздать. Если данные нужны:

```bash
alembic stamp head       # только записывает версию в alembic_version, таблицы не трогает
alembic check            # должно быть: No new upgrade operations detected.
```

Если `alembic check` нашёл расхождения, dev-схема отличается от моделей. Тогда `alembic stamp base`
(отменяет разметку) и либо пересоздайте базу, либо приведите схему в порядок вручную.

---

## 2. Прод на VPS (первый переход на Alembic)

Ситуация: таблицы с реальными данными уже есть, `group_message_id` добавлен вручную, `alembic_version`
нет. Baseline-миграцию здесь **не применяем**, а **помечаем как уже применённую** (`stamp`). Так Alembic
ничего не создаёт и не падает на существующих таблицах. Копируйте команды по порядку.

### 2.1. Обновить код и собрать образ (бот пока работает на старом)

```bash
cd /opt/PythonProject
git pull
docker compose build bot
```

### 2.2. Остановить бота и сделать бэкап

```bash
docker compose stop bot
./scripts/backup_db.sh
ls -lh backups/ | tail -3
```

Убедитесь, что появился свежий архив. Восстановление описано в `BACKUP_RESTORE.md`.

### 2.3. Сверить схему прода с baseline

Создаём во временной базе эталонную схему через baseline и сравниваем со схемой прода.

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "CREATE DATABASE schema_ref"'

docker compose run --rm bot sh -c 'DATABASE_URL="postgresql+asyncpg://$POSTGRES_USER:$POSTGRES_PASSWORD@db:5432/schema_ref" alembic upgrade head'

docker compose exec -T db sh -c 'pg_dump --schema-only --no-owner -T alembic_version -U "$POSTGRES_USER" -d "$POSTGRES_DB"' | grep -vE '^(--|\\restrict|\\unrestrict)' > /tmp/schema_prod.sql
docker compose exec -T db sh -c 'pg_dump --schema-only --no-owner -T alembic_version -U "$POSTGRES_USER" -d schema_ref' | grep -vE '^(--|\\restrict|\\unrestrict)' > /tmp/schema_ref.sql

diff /tmp/schema_prod.sql /tmp/schema_ref.sql && echo "СХЕМЫ СОВПАДАЮТ"
```

- **`СХЕМЫ СОВПАДАЮТ`** — переходите к 2.4.
- **Есть diff** — чаще всего он в `group_message_id`, потому что ручной `ALTER TABLE` мог отличаться от
  модели. Модель: `integer`, NULL допустим, `UNIQUE` с именем `requests_group_message_id_key`. Например,
  если нет UNIQUE, сначала проверьте, что дублей нет, и добавьте ограничение:

  ```bash
  docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT group_message_id, count(*) FROM requests WHERE group_message_id IS NOT NULL GROUP BY 1 HAVING count(*) > 1;"'
  # пусто -> можно добавлять:
  docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER TABLE requests ADD CONSTRAINT requests_group_message_id_key UNIQUE (group_message_id);"'
  ```

  Затем повторите `pg_dump` и `diff`, пока не будет `СХЕМЫ СОВПАДАЮТ`. Разницу, которую вы не понимаете,
  сначала разберите, и только потом делайте stamp.

Удалить временную базу:

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "DROP DATABASE schema_ref"'
```

### 2.4. Разметить прод-базу

```bash
docker compose run --rm bot alembic stamp head
docker compose run --rm bot alembic current     # -> 22fe4fd8bb35 (head)
docker compose run --rm bot alembic check       # -> No new upgrade operations detected.
```

`stamp` только создаёт таблицу `alembic_version` и записывает в неё номер ревизии. Данные и остальные
таблицы он не трогает. Отменить разметку: `docker compose run --rm bot alembic stamp base`.

### 2.5. Запустить бота

```bash
docker compose up -d bot
docker compose logs --tail 30 bot
```

В логах должно быть `Database migrations applied`, затем обычный старт polling. Проверьте, что данные
на месте:

```bash
docker compose exec -T db sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT (SELECT count(*) FROM users) AS users, (SELECT count(*) FROM requests) AS requests, (SELECT version_num FROM alembic_version) AS alembic;"'
```

Эта процедура нужна **один раз**. Дальше миграции накатываются сами при старте бота.

---

## 3. Новые миграции при изменении моделей

### 3.1. Создать

1. Измените `app/models.py`.
2. Убедитесь, что локальная база на текущем head (`alembic current`) и её схема совпадает с продом.
3. Сгенерируйте миграцию:

   ```bash
   alembic revision --autogenerate -m "add service type to requests"
   ```

### 3.2. Прочитать файл миграции глазами — обязательно

Autogenerate — только черновик. Что проверить:

- **Переименования.** Autogenerate их не распознаёт: переименование колонки или таблицы выглядит как
  `drop_column` + `add_column`, и **данные теряются**. Замените на
  `op.alter_column(..., new_column_name=...)` / `op.rename_table(...)`.
- **NOT NULL-колонка в таблице с данными.** `add_column(..., nullable=False)` упадёт на проде, если
  строки уже есть. Нужен `server_default=...` или три шага: добавить nullable → заполнить `UPDATE` →
  `alter_column(nullable=False)`.
- **Значения по умолчанию.** `default=` в модели работает только в Python, в БД не попадает. Если
  значение нужно в самой базе, укажите `server_default` явно.
- **UNIQUE и FK на существующих данных.** Перед добавлением проверьте, нет ли дублей или «висячих»
  ссылок на проде.
- **Миграции данных** (например, разнести `Requests.type` по новым значениям) autogenerate не пишет —
  добавьте `op.execute(...)` вручную.
- **Смена типа.** Отслеживается (`compare_type=True`), но проверьте, что Postgres сможет привести
  существующие значения (иногда нужен `postgresql_using=`).
- **`downgrade()`** — корректно откатывает `upgrade()`.
- Лишние операции, которых вы не ожидали: удаление таблиц или ограничений, о которых модели не знают.

### 3.3. Проверить локально перед `upgrade head` на проде

```bash
alembic upgrade head
alembic check              # No new upgrade operations detected. — модели и схема совпадают
alembic downgrade -1       # откат работает
alembic upgrade head       # и повторный накат тоже
alembic upgrade head --sql # (по желанию) посмотреть итоговый SQL
python -m e2e.run_e2e      # e2e сам пересоздаёт схему через миграции
```

Коммитьте файл миграции **в том же коммите**, что и изменение моделей.

### 3.4. Выкатить на прод

```bash
cd /opt/PythonProject
git pull
docker compose build bot
./scripts/backup_db.sh                 # бэкап перед любым изменением схемы
docker compose up -d bot               # upgrade head выполнится при старте
docker compose logs --tail 30 bot      # Database migrations applied
docker compose run --rm bot alembic current
```

Миграция идёт в одной транзакции (DDL в Postgres транзакционный): если она упала, схема остаётся
прежней, а бот не стартует, и ошибка видна в логах и в Sentry. Исправьте миграцию, пересоберите и
перезапустите.

### Не делайте

- Не меняйте схему прода руками через `ALTER TABLE`. Любое изменение — только через миграцию, иначе
  Alembic и реальная схема разойдутся.
- Не редактируйте миграцию, которая уже применена на проде. Нужна правка — создайте новую.
- Не возвращайте `create_all`: он создаёт таблицы в обход `alembic_version` и конфликтует с миграциями.
