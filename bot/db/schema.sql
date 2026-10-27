-- Схема базы данных бота ПК-клуба (SQLite).
-- Файл применяется автоматически при старте бота и при `python main.py --init-db`
-- (все объекты создаются через IF NOT EXISTS, поэтому запуск идемпотентен).
-- Для уже существующих баз недостающие столбцы и таблицы добавляет миграция в database.py.

-- Пользователи: согласие на обработку ПДн (152-ФЗ) хранится здесь же.
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,             -- Telegram ID
    username      TEXT,                            -- @username (может отсутствовать)
    first_name    TEXT,                            -- имя из профиля Telegram
    consent_given BOOLEAN NOT NULL DEFAULT 0,      -- 1 = согласие на обработку ПДн дано
    created_at    DATETIME                         -- ISO-8601 в часовом поясе клуба
);

-- Точки сети компьютерных клубов.
CREATE TABLE IF NOT EXISTS branches (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT    NOT NULL,                   -- название точки («NOVA ARENA · Орион»)
    city       TEXT    NOT NULL DEFAULT '',
    address    TEXT    NOT NULL DEFAULT '',
    hours      TEXT    NOT NULL DEFAULT 'круглосуточно, 24/7',
    phone      TEXT    NOT NULL DEFAULT '',
    is_active  BOOLEAN NOT NULL DEFAULT 1,
    created_at DATETIME
);

-- Заявки на бронирование.
CREATE TABLE IF NOT EXISTS bookings (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id          INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    tariff           TEXT    NOT NULL,             -- Standart / VIP / S VIP / Double
    time_slot        TEXT    NOT NULL,             -- «20:00–23:00», «На ночь (22:00–08:00)», свой текст
    client_name      TEXT    NOT NULL,             -- имя, на которое оформлена бронь
    receipt_file_id  TEXT,                         -- file_id фото или PDF чека
    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at       DATETIME,                     -- ISO-8601 в часовом поясе клуба
    decided_by       INTEGER,                      -- кто из администраторов принял решение
    decided_at       DATETIME,                     -- когда принято решение
    branch_id        INTEGER DEFAULT 1,            -- точка клуба
    booking_date     TEXT,                         -- YYYY-MM-DD (дата визита)
    slot_start       TEXT,                         -- HH:MM начала слота
    slot_end         TEXT,                         -- HH:MM окончания слота
    guests_count     INTEGER NOT NULL DEFAULT 1,   -- количество гостей
    seat_label       TEXT,                         -- номер ПК / комната («ПК #4»)
    guest_comment    TEXT,                         -- комментарий гостя
    guest_contact    TEXT,                         -- телефон / контакт (при ручной записи)
    extras_json      TEXT    NOT NULL DEFAULT '[]',-- выбранные доп. услуги (JSON-список ключей)
    quoted_price     INTEGER,                      -- рассчитанная сумма к оплате (₽)
    receipt_kind     TEXT    NOT NULL DEFAULT 'photo', -- 'photo' | 'document' (PDF)
    reject_reason    TEXT,                         -- no_seats | unpaid | guest_canceled | ...
    reject_comment   TEXT,                         -- комментарий администратора к отказу/отмене
    was_approved     BOOLEAN NOT NULL DEFAULT 0,   -- 1 = отменена после подтверждения
    no_show          BOOLEAN NOT NULL DEFAULT 0,   -- 1 = гость не пришёл
    created_by_admin BOOLEAN NOT NULL DEFAULT 0    -- 1 = создана админом вручную
);

-- Администраторы клуба (роли owner / operator / viewer, точка и настройки уведомлений).
-- Намеренно без FOREIGN KEY на users: администратора можно добавить по ID ещё до
-- того, как он написал боту.
CREATE TABLE IF NOT EXISTS admins (
    user_id       INTEGER PRIMARY KEY,             -- Telegram ID администратора
    role          TEXT    NOT NULL DEFAULT 'operator'
                  CHECK (role IN ('owner', 'operator', 'admin', 'viewer')),
    is_active     BOOLEAN NOT NULL DEFAULT 1,      -- деактивированный админ остаётся в истории
    added_by      INTEGER,                         -- кто добавил (NULL — добавлен через .env)
    created_at    DATETIME,
    display_name  TEXT,                            -- имя сотрудника
    branch_id     INTEGER,                         -- закреплённая точка (NULL — все / основная)
    notify_new    BOOLEAN NOT NULL DEFAULT 1,      -- пуш при новой брони
    notify_cancel BOOLEAN NOT NULL DEFAULT 1,      -- пуш при отмене гостем
    notify_noshow BOOLEAN NOT NULL DEFAULT 1,      -- пуш при «гость не пришёл»
    quiet_start   TEXT,                            -- начало тихих часов («23:00»)
    quiet_end     TEXT                             -- конец тихих часов («08:00»)
);

-- Одноразовые ссылки-приглашения для добавления администраторов.
CREATE TABLE IF NOT EXISTS admin_invites (
    token      TEXT PRIMARY KEY,
    name       TEXT    NOT NULL DEFAULT '',
    role       TEXT    NOT NULL DEFAULT 'operator',
    branch_id  INTEGER,
    created_by INTEGER,
    used_by    INTEGER,
    created_at DATETIME,
    used_at    DATETIME
);

-- Копии уведомления о заявке, отправленные каждому администратору.
CREATE TABLE IF NOT EXISTS booking_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings (id) ON DELETE CASCADE,
    admin_id   INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at DATETIME,
    UNIQUE (booking_id, admin_id, message_id)
);

-- Рабочие часы клуба по дням недели (0 = ПН ... 6 = ВС).
CREATE TABLE IF NOT EXISTS schedule_days (
    weekday    INTEGER PRIMARY KEY CHECK (weekday BETWEEN 0 AND 6),
    open_time  TEXT    NOT NULL DEFAULT '00:00',
    close_time TEXT    NOT NULL DEFAULT '24:00',
    is_closed  BOOLEAN NOT NULL DEFAULT 0
);

-- Особые часы работы в праздничные и спецдаты ('YYYY-MM-DD' или 'MM-DD').
CREATE TABLE IF NOT EXISTS schedule_holidays (
    holiday_date TEXT PRIMARY KEY,
    title        TEXT    NOT NULL DEFAULT 'Праздничный день',
    open_time    TEXT    NOT NULL DEFAULT '12:00',
    close_time   TEXT    NOT NULL DEFAULT '24:00',
    is_closed    BOOLEAN NOT NULL DEFAULT 0
);

-- Занятые слоты из внешних систем (Google Sheets / IZI).
CREATE TABLE IF NOT EXISTS external_busy_slots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    branch_id   INTEGER NOT NULL DEFAULT 1,
    slot_date   TEXT    NOT NULL,                  -- YYYY-MM-DD
    start_time  TEXT    NOT NULL,                  -- HH:MM
    end_time    TEXT    NOT NULL,                  -- HH:MM
    seats_taken INTEGER NOT NULL DEFAULT 16,
    source      TEXT    NOT NULL DEFAULT 'izi',    -- sheets | izi | manual
    note        TEXT    NOT NULL DEFAULT ''
);

-- Динамический каталог тарифов по залам (будни и выходные).
CREATE TABLE IF NOT EXISTS tariffs_catalog (
    key           TEXT PRIMARY KEY,
    title         TEXT    NOT NULL,
    emoji         TEXT    NOT NULL DEFAULT '🖥',
    pcs           INTEGER,
    hour1         INTEGER NOT NULL,
    hour1_weekend INTEGER NOT NULL,
    hour3         INTEGER NOT NULL,
    hour3_weekend INTEGER NOT NULL,
    hour5         INTEGER NOT NULL,
    hour5_weekend INTEGER NOT NULL,
    morning       INTEGER NOT NULL,
    day           INTEGER NOT NULL,
    night         INTEGER NOT NULL,
    night_weekend INTEGER NOT NULL,
    description   TEXT    NOT NULL DEFAULT '',
    sort_order    INTEGER NOT NULL DEFAULT 0,
    is_active     BOOLEAN NOT NULL DEFAULT 1
);

-- Дополнительные услуги к бронированию («свой ПК», «VIP-зал», «напиток» и др.).
CREATE TABLE IF NOT EXISTS tariff_extras (
    key       TEXT PRIMARY KEY,
    title     TEXT    NOT NULL,
    emoji     TEXT    NOT NULL DEFAULT '✨',
    price     INTEGER NOT NULL DEFAULT 0,
    is_active BOOLEAN NOT NULL DEFAULT 1
);

-- Живые настройки клуба (название, чат, реквизиты, приветствие, правила брони, шаг слота, буфер).
CREATE TABLE IF NOT EXISTS club_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at DATETIME
);

-- Индексы.
CREATE INDEX IF NOT EXISTS idx_bookings_status     ON bookings (status, id);
CREATE INDEX IF NOT EXISTS idx_bookings_user       ON bookings (user_id, id);
CREATE INDEX IF NOT EXISTS idx_users_consent       ON users (consent_given);
CREATE INDEX IF NOT EXISTS idx_admins_active       ON admins (is_active);
CREATE INDEX IF NOT EXISTS idx_notifications_bk    ON booking_notifications (booking_id);
CREATE INDEX IF NOT EXISTS idx_external_busy_date  ON external_busy_slots (slot_date, branch_id);
