-- Схема базы данных бота ПК-клуба (SQLite).
-- Файл применяется автоматически при старте бота и при `python main.py --init-db`
-- (все объекты создаются через IF NOT EXISTS, поэтому запуск идемпотентен).

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
    created_at      DATETIME                       -- ISO-8601 в часовом поясе клуба
);

-- Индексы: админ-панель постоянно выбирает заявки по статусу.
CREATE INDEX IF NOT EXISTS idx_bookings_status    ON bookings (status, id);
CREATE INDEX IF NOT EXISTS idx_bookings_user      ON bookings (user_id, id);
CREATE INDEX IF NOT EXISTS idx_users_consent      ON users (consent_given);
