-- Схема базы данных бота ПК-клуба (SQLite).
-- Файл применяется автоматически при старте бота и при `python main.py --init-db`
-- (все объекты создаются через IF NOT EXISTS, поэтому запуск идемпотентен).
-- Для уже существующих баз недостающие столбцы добавляет миграция в database.py.

-- Пользователи: согласие на обработку ПДн (152-ФЗ) хранится здесь же.
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,             -- Telegram ID
    username      TEXT,                            -- @username (может отсутствовать)
    first_name    TEXT,                            -- имя из профиля Telegram
    consent_given BOOLEAN NOT NULL DEFAULT 0,      -- 1 = согласие на обработку ПДн дано
    created_at    DATETIME                         -- ISO-8601 в часовом поясе клуба
);

-- Заявки на бронирование.
CREATE TABLE IF NOT EXISTS bookings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users (user_id) ON DELETE CASCADE,
    tariff          TEXT    NOT NULL,              -- Standart / VIP / S VIP / Double
    time_slot       TEXT    NOT NULL,              -- «Сейчас (с 17:51)», «На ночь (22:00–08:00)», свой текст
    client_name     TEXT    NOT NULL,              -- имя, на которое оформлена бронь
    receipt_file_id TEXT,                          -- file_id самого большого фото чека
    status          TEXT    NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending', 'approved', 'rejected')),
    created_at      DATETIME,                      -- ISO-8601 в часовом поясе клуба
    decided_by      INTEGER,                       -- кто из администраторов принял решение
    decided_at      DATETIME                       -- когда принято решение
);

-- Администраторы клуба (несколько человек, роли и активность).
-- Намеренно без FOREIGN KEY на users: администратора можно добавить по ID ещё до
-- того, как он написал боту (то есть до появления строки в users).
CREATE TABLE IF NOT EXISTS admins (
    user_id    INTEGER PRIMARY KEY,                -- Telegram ID администратора
    role       TEXT    NOT NULL DEFAULT 'operator'
               CHECK (role IN ('owner', 'operator')),
    is_active  BOOLEAN NOT NULL DEFAULT 1,         -- отключённый админ не видит заявки
    added_by   INTEGER,                            -- кто добавил (NULL — добавлен через .env)
    created_at DATETIME
);

-- Копии уведомления о заявке, отправленные каждому администратору.
-- Нужны, чтобы после решения одного админа погасить кнопки у остальных.
CREATE TABLE IF NOT EXISTS booking_notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    booking_id INTEGER NOT NULL REFERENCES bookings (id) ON DELETE CASCADE,
    admin_id   INTEGER NOT NULL,                   -- кому отправили
    message_id INTEGER NOT NULL,                   -- id сообщения в чате этого админа
    created_at DATETIME,
    UNIQUE (booking_id, admin_id, message_id)
);

-- Индексы: админ-панель постоянно выбирает заявки по статусу.
CREATE INDEX IF NOT EXISTS idx_bookings_status    ON bookings (status, id);
CREATE INDEX IF NOT EXISTS idx_bookings_user      ON bookings (user_id, id);
-- idx_bookings_decided создаётся в database.py после мягкой миграции столбцов:
-- в базе прошлой версии столбца decided_by ещё нет, и индекс её бы сломал.
CREATE INDEX IF NOT EXISTS idx_users_consent      ON users (consent_given);
CREATE INDEX IF NOT EXISTS idx_admins_active      ON admins (is_active);
CREATE INDEX IF NOT EXISTS idx_notifications_bk   ON booking_notifications (booking_id);
