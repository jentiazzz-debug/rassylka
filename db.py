"""Хранилище: люди, подписки, подключённые аккаунты, запросы кодов.

Три вещи, из-за которых схема выглядит именно так:

* **Строка сессии лежит зашифрованной** (`accounts.session`). Её
  шифрует crypto.py, база про содержимое ничего не знает. Ключ живёт
  отдельно от базы — см. config.session_key.
* **Один номер — один аккаунт на человека**: UNIQUE (user_id, phone).
  Иначе повторный вход по тому же номеру заводил бы вторую запись, а
  первая оставалась бы в списке мёртвой копией.
* **Запросы кодов пишутся в базу, а не в память.** FloodWait за частые
  попытки входа Telegram выдаёт номеру на часы, а перезапуск процесса
  случается за это время не раз: счётчик в памяти обнулялся бы и
  отправлял человека долбить Telegram заново, продлевая себе же
  наказание.

Пробный период не хранится флагом «активен». Хранится дата конца, а
активность считается от текущего времени: флаг пришлось бы кому-то
гасить по расписанию, и любой пропущенный запуск раздавал бы людям
бесплатные дни.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import aiosqlite

import config

log = logging.getLogger("rassylka.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    name          TEXT,
    created_at    INTEGER NOT NULL,
    trial_ends_at INTEGER NOT NULL DEFAULT 0,
    paid_until    INTEGER NOT NULL DEFAULT 0,
    seen_at       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS accounts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    phone      TEXT    NOT NULL,
    tg_id      INTEGER,
    name       TEXT,
    username   TEXT,
    session    TEXT    NOT NULL,
    status     TEXT    NOT NULL DEFAULT 'ok',
    added_at   INTEGER NOT NULL,
    checked_at INTEGER NOT NULL DEFAULT 0,
    note       TEXT,
    UNIQUE (user_id, phone)
);
CREATE INDEX IF NOT EXISTS idx_accounts_user ON accounts(user_id);

CREATE TABLE IF NOT EXISTS code_requests (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    phone      TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_codes_user ON code_requests(user_id, created_at);
"""

_db: aiosqlite.Connection | None = None


async def connect() -> None:
    global _db
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    _db = await aiosqlite.connect(config.DB_PATH)
    _db.row_factory = aiosqlite.Row
    # WAL: веб-сервер мини-аппа и поллинг бота живут в одном процессе, но
    # пишут из разных задач, и на обычном журнале короткая запись одной
    # задачи блокирует чтение другой.
    await _db.execute("PRAGMA journal_mode=WAL")
    await _db.execute("PRAGMA foreign_keys=ON")
    await _db.executescript(SCHEMA)
    await _db.commit()
    log.info("база готова: %s", config.DB_PATH)


async def close() -> None:
    global _db
    if _db is not None:
        await _db.close()
        _db = None


def _conn() -> aiosqlite.Connection:
    if _db is None:
        raise RuntimeError("db.connect() не вызывали")
    return _db


# --- люди и подписка --------------------------------------------------


@dataclass
class Subscription:
    """Состояние подписки на момент вопроса."""

    #: trial — идёт пробный период, paid — оплачено, expired — кончилось.
    kind: str
    until: int

    @property
    def active(self) -> bool:
        return self.kind in {"trial", "paid"}

    @property
    def paid(self) -> bool:
        return self.kind == "paid"

    @property
    def seconds_left(self) -> int:
        return max(0, self.until - int(time.time()))

    @property
    def days_left(self) -> int:
        """Дней осталось, с округлением вверх.

        Вверх — потому что «осталось 0 дней» при живых двенадцати часах
        читается как «всё, кончилось», и человек идёт писать в поддержку.
        """
        left = self.seconds_left
        return -(-left // 86400) if left else 0


async def ensure_user(user_id: int, username: str | None, name: str | None) -> None:
    """Завести человека при первом /start и завести ему пробный период.

    Пробный период выдаётся ровно один раз — INSERT ... ON CONFLICT
    трогает только ник и время визита. Иначе /start после конца триала
    выдавал бы новые пять дней сколько угодно раз.
    """
    now = int(time.time())
    trial_until = now + config.TRIAL_DAYS * 86400
    await _conn().execute(
        """
        INSERT INTO users (user_id, username, name, created_at,
                           trial_ends_at, seen_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id) DO UPDATE SET
            username = excluded.username,
            name     = excluded.name,
            seen_at  = excluded.seen_at
        """,
        (user_id, username, name, now, trial_until, now),
    )
    await _conn().commit()


async def subscription(user_id: int) -> Subscription:
    row = await _fetchone(
        "SELECT trial_ends_at, paid_until FROM users WHERE user_id = ?", (user_id,)
    )
    if row is None:
        return Subscription(kind="expired", until=0)
    now = int(time.time())
    paid_until = int(row["paid_until"] or 0)
    trial_until = int(row["trial_ends_at"] or 0)
    if paid_until > now:
        return Subscription(kind="paid", until=paid_until)
    if trial_until > now:
        return Subscription(kind="trial", until=trial_until)
    # Показываем ту дату, которая была последней: человек спрашивает
    # «когда кончилось», а не «когда кончился триал».
    return Subscription(kind="expired", until=max(paid_until, trial_until))


async def grant_paid(user_id: int, days: int) -> int:
    """Продлить платную подписку. Возвращает новую дату конца.

    Продление считается от текущей даты конца, а не от «сейчас»: оплата
    за месяц, сделанная за неделю до конца, не должна съедать эту неделю.
    """
    now = int(time.time())
    row = await _fetchone("SELECT paid_until FROM users WHERE user_id = ?", (user_id,))
    base = max(now, int(row["paid_until"] or 0) if row else 0)
    until = base + days * 86400
    await _conn().execute(
        "UPDATE users SET paid_until = ? WHERE user_id = ?", (until, user_id)
    )
    await _conn().commit()
    return until


# --- аккаунты ---------------------------------------------------------


@dataclass
class Account:
    id: int
    user_id: int
    phone: str
    tg_id: int | None
    name: str | None
    username: str | None
    status: str
    added_at: int
    checked_at: int
    note: str | None
    #: Зашифрованная строка сессии. Наружу, в мини-апп, не уходит никогда.
    session: str = ""

    @property
    def title(self) -> str:
        return (self.name or "").strip() or self.phone


def _account(row: aiosqlite.Row) -> Account:
    return Account(
        id=row["id"],
        user_id=row["user_id"],
        phone=row["phone"],
        tg_id=row["tg_id"],
        name=row["name"],
        username=row["username"],
        status=row["status"],
        added_at=row["added_at"],
        checked_at=row["checked_at"],
        note=row["note"],
        session=row["session"] if "session" in row.keys() else "",
    )


async def accounts(user_id: int) -> list[Account]:
    rows = await _fetchall(
        """
        SELECT id, user_id, phone, tg_id, name, username, status,
               added_at, checked_at, note
        FROM accounts WHERE user_id = ? ORDER BY added_at
        """,
        (user_id,),
    )
    return [_account(row) for row in rows]


async def account(user_id: int, account_id: int) -> Account | None:
    """Один аккаунт вместе с сессией.

    user_id в условии, а не только id: иначе достаточно было бы подобрать
    чужой номер записи, чтобы получить чужую сессию.
    """
    row = await _fetchone(
        "SELECT * FROM accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
    )
    return _account(row) if row else None


async def count_accounts(user_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM accounts WHERE user_id = ?", (user_id,)
    )
    return int(row["n"]) if row else 0


async def save_account(
    user_id: int,
    phone: str,
    session: str,
    *,
    tg_id: int | None,
    name: str | None,
    username: str | None,
) -> int:
    """Записать удачный вход. Повторный вход по тому же номеру обновляет запись.

    Обновляет, а не добавляет: человек, у которого сессия умерла и он
    вошёл заново, ждёт увидеть один свой аккаунт, а не два — из которых
    один не работает.
    """
    now = int(time.time())
    await _conn().execute(
        """
        INSERT INTO accounts (user_id, phone, tg_id, name, username,
                              session, status, added_at, checked_at)
        VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?)
        ON CONFLICT (user_id, phone) DO UPDATE SET
            tg_id      = excluded.tg_id,
            name       = excluded.name,
            username   = excluded.username,
            session    = excluded.session,
            status     = 'ok',
            checked_at = excluded.checked_at,
            note       = NULL
        """,
        (user_id, phone, tg_id, name, username, session, now, now),
    )
    await _conn().commit()
    row = await _fetchone(
        "SELECT id FROM accounts WHERE user_id = ? AND phone = ?", (user_id, phone)
    )
    return int(row["id"]) if row else 0


async def mark_account(account_id: int, status: str, note: str | None = None) -> None:
    await _conn().execute(
        "UPDATE accounts SET status = ?, note = ?, checked_at = ? WHERE id = ?",
        (status, note, int(time.time()), account_id),
    )
    await _conn().commit()


async def delete_account(user_id: int, account_id: int) -> bool:
    cursor = await _conn().execute(
        "DELETE FROM accounts WHERE id = ? AND user_id = ?", (account_id, user_id)
    )
    await _conn().commit()
    return cursor.rowcount > 0


# --- тормоз на запросы кодов ------------------------------------------


async def code_requests_last_hour(user_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM code_requests "
        "WHERE user_id = ? AND created_at > ?",
        (user_id, int(time.time()) - 3600),
    )
    return int(row["n"]) if row else 0


async def log_code_request(user_id: int, phone: str) -> None:
    await _conn().execute(
        "INSERT INTO code_requests (user_id, phone, created_at) VALUES (?, ?, ?)",
        (user_id, phone, int(time.time())),
    )
    # Старые записи ни на что не влияют, а таблица растёт: чистим сразу,
    # отдельная уборка по расписанию для этого не нужна.
    await _conn().execute(
        "DELETE FROM code_requests WHERE created_at < ?", (int(time.time()) - 86400,)
    )
    await _conn().commit()


# --- статистика для админа --------------------------------------------


async def stats() -> dict[str, int]:
    now = int(time.time())
    row = await _fetchone(
        """
        SELECT
            (SELECT COUNT(*) FROM users) AS users,
            (SELECT COUNT(*) FROM users WHERE seen_at > ?) AS active_day,
            (SELECT COUNT(*) FROM users WHERE trial_ends_at > ?
                 AND paid_until <= ?) AS on_trial,
            (SELECT COUNT(*) FROM users WHERE paid_until > ?) AS paid,
            (SELECT COUNT(*) FROM accounts) AS accounts,
            (SELECT COUNT(*) FROM accounts WHERE status = 'ok') AS accounts_ok
        """,
        (now - 86400, now, now, now),
    )
    return {key: int(row[key] or 0) for key in row.keys()} if row else {}


# --- мелочи -----------------------------------------------------------


async def _fetchone(sql: str, args: tuple = ()) -> aiosqlite.Row | None:
    async with _conn().execute(sql, args) as cursor:
        return await cursor.fetchone()


async def _fetchall(sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
    async with _conn().execute(sql, args) as cursor:
        return list(await cursor.fetchall())
