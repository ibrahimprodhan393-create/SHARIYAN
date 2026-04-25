-- ============================================================
-- Telegram Shop Bot — Neon (Postgres) schema + data import
-- Paste this entire file into Neon's SQL Editor and click Run.
-- ============================================================

-- 1) DROP existing tables (uncomment if re-importing)
-- DROP TABLE IF EXISTS files, bot_settings, custom_pricing, transactions, orders, keys, plans, panels, users CASCADE;

-- 2) SCHEMA
CREATE TABLE IF NOT EXISTS users (
    telegram_id   BIGINT PRIMARY KEY,
    name          TEXT,
    phone         TEXT,
    balance       DOUBLE PRECISION DEFAULT 0,
    total_spent   DOUBLE PRECISION DEFAULT 0,
    total_deposit DOUBLE PRECISION DEFAULT 0,
    join_date     TEXT,
    status        TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS panels (
    id        SERIAL PRIMARY KEY,
    name      TEXT NOT NULL,
    platform  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id        SERIAL PRIMARY KEY,
    panel_id  INTEGER NOT NULL REFERENCES panels(id) ON DELETE CASCADE,
    name      TEXT NOT NULL,
    price     DOUBLE PRECISION NOT NULL
);

CREATE TABLE IF NOT EXISTS keys (
    id       SERIAL PRIMARY KEY,
    plan_id  INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
    key      TEXT NOT NULL,
    is_sold  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS orders (
    id       SERIAL PRIMARY KEY,
    user_id  BIGINT NOT NULL,
    plan_id  INTEGER NOT NULL,
    key      TEXT NOT NULL,
    date     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id       SERIAL PRIMARY KEY,
    user_id  BIGINT NOT NULL,
    amount   DOUBLE PRECISION NOT NULL,
    type     TEXT NOT NULL,
    date     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_pricing (
    user_id  BIGINT NOT NULL,
    plan_id  INTEGER NOT NULL,
    price    DOUBLE PRECISION NOT NULL,
    PRIMARY KEY (user_id, plan_id)
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id          SERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    mime        TEXT,
    uploaded_by BIGINT,
    date        TEXT NOT NULL
);

-- 3) DATA

-- users: 5 rows
INSERT INTO users (telegram_id, name, phone, balance, total_spent, total_deposit, join_date, status) VALUES (6999715659, 'Shariyan Xit', '8801823373439', 0.0, 0.0, 0.0, '2026-04-23 09:03:29', 'active');
INSERT INTO users (telegram_id, name, phone, balance, total_spent, total_deposit, join_date, status) VALUES (7083772670, '❄️ Knox', '919122879191', 100.0, 0.0, 100.0, '2026-04-23 09:03:25', 'active');
INSERT INTO users (telegram_id, name, phone, balance, total_spent, total_deposit, join_date, status) VALUES (8069745204, 'Ios_cheat', '966555324347', 0.0, 0.0, 0.0, '2026-04-23 17:44:18', 'active');
INSERT INTO users (telegram_id, name, phone, balance, total_spent, total_deposit, join_date, status) VALUES (8090143182, '𐌑ⲅ RED DOT FF', '966500284630', 0.0, 0.0, 0.0, '2026-04-23 17:24:48', 'active');
INSERT INTO users (telegram_id, name, phone, balance, total_spent, total_deposit, join_date, status) VALUES (8232879621, '❄️ 愛•| ᴅᴀʀᴋ ⚠️', '919234105752', 0.0, 0.0, 0.0, '2026-04-23 09:17:39', 'active');

-- panels: 1 rows
INSERT INTO panels (id, name, platform) VALUES (1, 'Pa', 'iOS');
SELECT setval(pg_get_serial_sequence('panels', 'id'), COALESCE((SELECT MAX(id) FROM panels), 1), true);

-- plans: 3 rows
INSERT INTO plans (id, panel_id, name, price) VALUES (1, 1, '1', 3.0);
INSERT INTO plans (id, panel_id, name, price) VALUES (2, 1, '7', 15.0);
INSERT INTO plans (id, panel_id, name, price) VALUES (3, 1, '31', 20.0);
SELECT setval(pg_get_serial_sequence('plans', 'id'), COALESCE((SELECT MAX(id) FROM plans), 1), true);

-- keys: 0 rows

-- orders: 0 rows

-- transactions: 1 rows
INSERT INTO transactions (id, user_id, amount, type, date) VALUES (1, 7083772670, 100.0, 'admin_add', '2026-04-23 09:18:53');
SELECT setval(pg_get_serial_sequence('transactions', 'id'), COALESCE((SELECT MAX(id) FROM transactions), 1), true);

-- custom_pricing: 0 rows

-- bot_settings: 0 rows

-- files: 0 rows
