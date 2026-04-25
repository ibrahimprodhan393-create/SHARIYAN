"""SQLite-compatible database wrapper.

This module lets ``bot.py`` keep using its existing sqlite3-flavoured queries
while transparently talking to PostgreSQL when ``DATABASE_URL`` is set in the
environment. That way the bot's data survives redeploys on hosts with
ephemeral filesystems (Render free tier, Heroku, Railway, Fly.io, etc.).

Behaviour:

* If ``DATABASE_URL`` is set, every ``db()`` call opens a fresh psycopg
  (v3) connection to that PostgreSQL database. Queries written with SQLite
  syntax (``?`` placeholders, ``INSERT OR REPLACE``, ``BEGIN IMMEDIATE``,
  ``executescript`` and ``PRAGMA …``) are translated on the fly. Rows
  returned support both ``r["name"]`` and ``r[0]`` access just like
  ``sqlite3.Row``.
* Otherwise we fall back to the original SQLite file at ``DB_PATH``
  (``shop.db``), which keeps local development working unchanged.
"""

from __future__ import annotations

import os
import re
import sqlite3
from contextlib import contextmanager

DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
DB_PATH = os.environ.get("DB_PATH", "shop.db")
USE_PG = bool(DATABASE_URL)

_PG = None
_DICT_ROW = None
if USE_PG:
    # psycopg (v3) ships prebuilt binary wheels for current Python versions
    # including 3.14, which psycopg2-binary still lacks.
    import psycopg as _PG
    from psycopg.rows import dict_row as _DICT_ROW

# Tables whose primary key is an auto-increment ``id`` column. INSERTs into
# these tables get ``RETURNING id`` appended automatically so callers can
# read ``cursor.lastrowid`` (PostgreSQL has no implicit lastrowid).
_AUTOID_TABLES = {"panels", "plans", "keys", "orders", "transactions", "files"}


# ---------------------------------------------------------------------------
# SQL translation helpers
# ---------------------------------------------------------------------------
def _replace_qmarks(sql: str) -> str:
    """Replace ``?`` placeholders with ``%s`` outside of string literals."""
    out, in_str, quote = [], False, None
    for ch in sql:
        if in_str:
            out.append(ch)
            if ch == quote:
                in_str = False
                quote = None
        else:
            if ch in ("'", '"'):
                in_str = True
                quote = ch
                out.append(ch)
            elif ch == "?":
                out.append("%s")
            else:
                out.append(ch)
    return "".join(out)


def _translate_query(sql: str):
    """Translate a runtime query for PostgreSQL.

    Returns ``(translated_sql, kind)`` where ``kind`` is one of
    ``"sql"``, ``"begin"``, ``"commit"``, ``"rollback"`` or ``"noop"``.
    """
    stripped = sql.strip().rstrip(";").strip()
    upper = stripped.upper()

    if upper.startswith("PRAGMA"):
        return "", "noop"
    if upper in ("BEGIN", "BEGIN IMMEDIATE", "BEGIN DEFERRED", "BEGIN EXCLUSIVE"):
        return "", "begin"
    if upper in ("COMMIT", "END"):
        return "", "commit"
    if upper == "ROLLBACK":
        return "", "rollback"

    # The two ``INSERT OR REPLACE`` sites in bot.py both have well-known
    # conflict targets, so we can rewrite them as proper UPSERTs.
    m = re.match(
        r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+bot_settings\s*\(\s*key\s*,\s*value\s*\)\s*"
        r"VALUES\s*\((.+)\)\s*$",
        stripped, flags=re.I | re.S,
    )
    if m:
        return _replace_qmarks(
            "INSERT INTO bot_settings(key,value) VALUES("
            + m.group(1)
            + ") ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value"
        ), "sql"

    m = re.match(
        r"\s*INSERT\s+OR\s+REPLACE\s+INTO\s+custom_pricing\s*\(\s*user_id\s*,\s*plan_id\s*,\s*price\s*\)\s*"
        r"VALUES\s*\((.+)\)\s*$",
        stripped, flags=re.I | re.S,
    )
    if m:
        return _replace_qmarks(
            "INSERT INTO custom_pricing(user_id,plan_id,price) VALUES("
            + m.group(1)
            + ") ON CONFLICT (user_id, plan_id) DO UPDATE SET price=EXCLUDED.price"
        ), "sql"

    sql_t = _replace_qmarks(stripped)

    # If this is an INSERT into a table with an auto-increment id, append
    # ``RETURNING id`` (unless one is already there) so we can populate
    # ``cursor.lastrowid``.
    m = re.match(r"\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", sql_t, flags=re.I)
    if m and m.group(1).lower() in _AUTOID_TABLES and "RETURNING" not in sql_t.upper():
        sql_t = sql_t + " RETURNING id"

    return sql_t, "sql"


def _translate_schema(sql: str) -> str:
    """Translate a CREATE TABLE/INDEX statement to PostgreSQL."""
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT",
        "BIGSERIAL PRIMARY KEY", sql, flags=re.I,
    )
    sql = re.sub(r"\bAUTOINCREMENT\b", "", sql, flags=re.I)
    # ``users.telegram_id`` uses ``INTEGER PRIMARY KEY`` (no autoincrement)
    # but Telegram IDs can exceed 2^31, so widen to BIGINT.
    sql = re.sub(
        r"INTEGER\s+PRIMARY\s+KEY(?!\s+AUTOINCREMENT)",
        "BIGINT PRIMARY KEY", sql, flags=re.I,
    )
    sql = re.sub(r"\bREAL\b", "DOUBLE PRECISION", sql, flags=re.I)
    return sql


def _split_statements(script: str):
    """Split a multi-statement SQL script on ``;``.

    The bot's schema has no semicolons inside string literals so a naive
    split is sufficient.
    """
    return [s.strip() for s in script.split(";") if s.strip()]


# ---------------------------------------------------------------------------
# PostgreSQL row / cursor / connection wrappers
# ---------------------------------------------------------------------------
class _PGRow:
    """Dict + integer-index row, mimicking ``sqlite3.Row``."""

    __slots__ = ("_d", "_keys")

    def __init__(self, d):
        self._d = d
        self._keys = list(d.keys())

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._d[self._keys[k]]
        return self._d[k]

    def keys(self):
        return list(self._keys)

    def __iter__(self):
        for k in self._keys:
            yield self._d[k]

    def get(self, k, default=None):
        return self._d.get(k, default)

    def __repr__(self):
        return f"_PGRow({self._d!r})"


class _NoOpCursor:
    rowcount = -1
    lastrowid = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _PGCursor:
    def __init__(self, raw, lastrowid=None):
        self._raw = raw
        self._lastrowid = lastrowid

    def fetchone(self):
        try:
            r = self._raw.fetchone()
        except _PG.ProgrammingError:
            return None
        return _PGRow(r) if r is not None else None

    def fetchall(self):
        try:
            rows = self._raw.fetchall()
        except _PG.ProgrammingError:
            return []
        return [_PGRow(r) for r in rows]

    @property
    def rowcount(self):
        return self._raw.rowcount

    @property
    def lastrowid(self):
        return self._lastrowid


class _PGConn:
    def __init__(self, raw):
        self._raw = raw
        # Default to autocommit so that simple ``c.execute("UPDATE …")``
        # calls commit immediately, matching sqlite3 with
        # ``isolation_level=None``. Explicit ``BEGIN IMMEDIATE`` /
        # ``COMMIT`` / ``ROLLBACK`` from bot.py temporarily disables it.
        self._raw.autocommit = True
        self._in_tx = False

    def execute(self, sql, params=()):
        translated, kind = _translate_query(sql)
        if kind == "noop":
            return _NoOpCursor()
        if kind == "begin":
            if not self._in_tx:
                self._raw.autocommit = False
                self._in_tx = True
            return _NoOpCursor()
        if kind == "commit":
            try:
                self._raw.commit()
            finally:
                self._raw.autocommit = True
                self._in_tx = False
            return _NoOpCursor()
        if kind == "rollback":
            try:
                self._raw.rollback()
            finally:
                self._raw.autocommit = True
                self._in_tx = False
            return _NoOpCursor()

        cur = self._raw.cursor(row_factory=_DICT_ROW)
        cur.execute(translated, tuple(params))
        last = None
        if "RETURNING ID" in translated.upper():
            try:
                row = cur.fetchone()
                if row is not None:
                    last = row.get("id")
            except _PG.ProgrammingError:
                pass
        return _PGCursor(cur, last)

    def executescript(self, script):
        for stmt in _split_statements(script):
            cur = self._raw.cursor()
            cur.execute(_translate_schema(stmt))
            cur.close()

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    telegram_id   INTEGER PRIMARY KEY,
    name          TEXT,
    phone         TEXT,
    balance       REAL DEFAULT 0,
    total_spent   REAL DEFAULT 0,
    total_deposit REAL DEFAULT 0,
    join_date     TEXT,
    status        TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS panels (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    name      TEXT NOT NULL,
    platform  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plans (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    panel_id  INTEGER NOT NULL,
    name      TEXT NOT NULL,
    price     REAL NOT NULL,
    FOREIGN KEY(panel_id) REFERENCES panels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS keys (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id  INTEGER NOT NULL,
    key      TEXT NOT NULL,
    is_sold  INTEGER NOT NULL DEFAULT 0,
    FOREIGN KEY(plan_id) REFERENCES plans(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS orders (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    plan_id  INTEGER NOT NULL,
    key      TEXT NOT NULL,
    date     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS transactions (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    amount   REAL NOT NULL,
    type     TEXT NOT NULL,
    date     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS custom_pricing (
    user_id  INTEGER NOT NULL,
    plan_id  INTEGER NOT NULL,
    price    REAL NOT NULL,
    PRIMARY KEY (user_id, plan_id)
);

CREATE TABLE IF NOT EXISTS bot_settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    file_id     TEXT NOT NULL,
    mime        TEXT,
    uploaded_by INTEGER,
    date        TEXT NOT NULL
);
"""


@contextmanager
def db():
    """Yield a connection-like object; works for both PostgreSQL and SQLite."""
    if USE_PG:
        conn = _PG.connect(DATABASE_URL)
        wrapper = _PGConn(conn)
        try:
            yield wrapper
        finally:
            try:
                wrapper.close()
            except Exception:
                pass
    else:
        conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()


def init_db():
    with db() as c:
        c.executescript(SCHEMA)
