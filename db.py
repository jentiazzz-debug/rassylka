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

import json
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
    seen_at       INTEGER NOT NULL DEFAULT 0,
    -- Внутренняя валюта: ею платят за подписку. Приходит от покупки за
    -- звёзды и от приглашённых.
    coins         INTEGER NOT NULL DEFAULT 0,
    -- Кто пригласил. Ставится один раз при первом /start и больше не
    -- меняется: иначе приглашённого можно было бы «переприсвоить».
    ref_by        INTEGER
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
    -- Ключи и параметры устройства, с которыми аккаунт заходил (JSON).
    -- Пусто — вход по номеру нашими общими ключами. У аккаунта из tdata
    -- ключи чужие: подключаться к нему нашими нельзя, Telegram Desktop
    -- выдавал сессию под свой api_id, и смена ключа на живой сессии —
    -- прямой путь к её отзыву.
    api        TEXT,
    -- phone | tdata: чем аккаунт подключали. Нужно только для показа.
    source     TEXT    NOT NULL DEFAULT 'phone',
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

-- Кэш диалогов аккаунта. access_hash хранится не для красоты: строка
-- сессии несёт только ключ авторизации, справочник знакомых чатов в неё
-- не входит. Без сохранённого хэша отправка по одному id требовала бы
-- каждый раз заново вычитывать все диалоги — это лишний тяжёлый запрос
-- перед каждым сообщением и лишний повод для FloodWait.
CREATE TABLE IF NOT EXISTS chats (
    account_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    raw_id      INTEGER NOT NULL,
    access_hash INTEGER,
    kind        TEXT    NOT NULL,
    -- Канал или супергруппа: у Telegram это один тип, различает их
    -- только флаг. В списке чатов их надо показывать по-разному.
    broadcast   INTEGER NOT NULL DEFAULT 0,
    title       TEXT,
    username    TEXT,
    scanned_at  INTEGER NOT NULL,
    PRIMARY KEY (account_id, chat_id)
);

CREATE TABLE IF NOT EXISTS folders (
    account_id INTEGER NOT NULL,
    folder_id  INTEGER NOT NULL,
    title      TEXT,
    chat_ids   TEXT    NOT NULL,
    scanned_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, folder_id)
);

CREATE TABLE IF NOT EXISTS campaigns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    account_id  INTEGER NOT NULL,
    title       TEXT,
    text        TEXT    NOT NULL,
    interval    INTEGER NOT NULL,
    source      TEXT    NOT NULL DEFAULT 'chats',
    folder_id   INTEGER,
    -- text — берём текст из поля text; saved — копируем сообщение из
    -- «Избранного» аккаунта, вместе с медиа и оформлением.
    content     TEXT    NOT NULL DEFAULT 'text',
    saved_id    INTEGER,
    -- order — варианты по очереди, random — вразнобой.
    pick        TEXT    NOT NULL DEFAULT 'random',
    text_cursor INTEGER NOT NULL DEFAULT 0,
    status      TEXT    NOT NULL DEFAULT 'running',
    cursor      INTEGER NOT NULL DEFAULT 0,
    next_run_at INTEGER NOT NULL DEFAULT 0,
    sent_ok     INTEGER NOT NULL DEFAULT 0,
    sent_err    INTEGER NOT NULL DEFAULT 0,
    cycles      INTEGER NOT NULL DEFAULT 0,
    note        TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_campaigns_user ON campaigns(user_id);
CREATE INDEX IF NOT EXISTS idx_campaigns_due  ON campaigns(status, next_run_at);

CREATE TABLE IF NOT EXISTS campaign_targets (
    campaign_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    PRIMARY KEY (campaign_id, chat_id)
);

CREATE TABLE IF NOT EXISTS sends (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    account_id  INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    title       TEXT,
    ok          INTEGER NOT NULL,
    error       TEXT,
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sends_campaign ON sends(campaign_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sends_account  ON sends(account_id, created_at);

-- Оплаты звёздами. Журнал, а не счётчик: по нему видно, за что и когда
-- продлевали, и он же защищает от повторного зачисления — Telegram
-- присылает успешный платёж повторно, если бот не ответил вовремя.
CREATE TABLE IF NOT EXISTS payments (
    charge_id  TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL,
    stars      INTEGER NOT NULL,
    days       INTEGER NOT NULL,
    payload    TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id, created_at);

-- Счета на оплату рублями. Заводятся до того, как человек уйдёт
-- платить: начисление опирается на эту запись, а не на то, что придёт в
-- callback снаружи. Подписи у callback нет, и доверять его числам
-- нельзя — иначе подделанный запрос начислит сколько попросит.
CREATE TABLE IF NOT EXISTS invoices (
    transaction_id TEXT PRIMARY KEY,
    order_id       TEXT,
    user_id        INTEGER NOT NULL,
    coins          INTEGER NOT NULL,
    rub            REAL    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'pending',
    -- platega | crypto: у счетов разные способы проверки, и сверка
    -- каждого провайдера ходит только за своими.
    provider       TEXT    NOT NULL DEFAULT 'platega',
    created_at     INTEGER NOT NULL,
    closed_at      INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_invoices_pending ON invoices(status, created_at);
CREATE INDEX IF NOT EXISTS idx_invoices_user ON invoices(user_id, created_at);

-- Настройки, которые владелец меняет из бота, а не через .env:
-- приветствие, кнопки меню, баннер в приложении. Ключ-значение, потому
-- что набор таких настроек будет расти, а заводить колонку на каждую —
-- это миграция на каждый чих.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    updated_at INTEGER NOT NULL
);

-- Варианты сообщения одной рассылки. Одинаковый текст, уходящий по
-- кругу в сотню чатов, — самый заметный признак рассылки из всех, и
-- ловится он тривиально. Несколько вариантов вперемешку такую проверку
-- уже не проходят.
CREATE TABLE IF NOT EXISTS campaign_texts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id INTEGER NOT NULL,
    position    INTEGER NOT NULL,
    content     TEXT    NOT NULL DEFAULT 'text',
    text        TEXT,
    saved_id    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_texts_campaign
    ON campaign_texts(campaign_id, position);

-- Попытки подписаться на чат. Пишутся в базу, чтобы не долбиться в один
-- и тот же закрытый чат каждый круг: Telegram считает вступления
-- отдельно от сообщений и наказывает за них так же.
CREATE TABLE IF NOT EXISTS joins (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    chat_id    INTEGER NOT NULL,
    ok         INTEGER NOT NULL,
    error      TEXT,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_joins_account ON joins(account_id, created_at);
CREATE INDEX IF NOT EXISTS idx_joins_chat ON joins(account_id, chat_id, created_at);

-- Движения по монетам. Журнал, а не только баланс: человек должен
-- видеть, откуда монеты взялись и куда делись, а мы — уметь разобрать
-- спор, не гадая по остатку.
CREATE TABLE IF NOT EXISTS coin_ops (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    delta      INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_coin_ops_user ON coin_ops(user_id, created_at);

-- Материалы: сообщения из «Избранного» подключённого аккаунта. Само
-- сообщение здесь не хранится — только id и то, что нужно показать в
-- списке. Отправляется оно копированием прямо из «Избранного», и
-- поэтому переживает всё: премиум-эмодзи, стикеры, цитаты, альбомы.
CREATE TABLE IF NOT EXISTS materials (
    account_id INTEGER NOT NULL,
    msg_id     INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    preview    TEXT,
    has_media  INTEGER NOT NULL DEFAULT 0,
    scanned_at INTEGER NOT NULL,
    PRIMARY KEY (account_id, msg_id)
);

-- Паузы аккаунтов по FloodWait. В базе, а не в памяти: Telegram выдаёт
-- их на часы, а передеплой за это время случается не раз — счётчик в
-- памяти обнулялся бы и отправлял аккаунт продлевать себе наказание.
CREATE TABLE IF NOT EXISTS account_pauses (
    account_id INTEGER PRIMARY KEY,
    until      INTEGER NOT NULL,
    reason     TEXT
);
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
    await _migrate()
    await _db.commit()
    log.info("база готова: %s", config.DB_PATH)


async def _migrate() -> None:
    """Доращивание схемы на уже живой базе.

    CREATE TABLE IF NOT EXISTS новых колонок не добавляет: у тех, кто
    обновился, таблица уже есть, и новое поле в ней не появится само.
    Без этого обновление роняет бота на первом же запросе к колонке,
    которой нет, — причём только на боевой базе, а не на чистой.
    """
    await _ensure_column(
        "accounts",
        "api",
        # Ключи и параметры устройства, с которыми заходили. Для входа по
        # номеру пусто — там наши общие ключи из .env.
        "ALTER TABLE accounts ADD COLUMN api TEXT",
    )
    await _ensure_column(
        "accounts",
        "source",
        "ALTER TABLE accounts ADD COLUMN source TEXT NOT NULL DEFAULT 'phone'",
    )
    await _ensure_column(
        "users", "coins", "ALTER TABLE users ADD COLUMN coins INTEGER NOT NULL DEFAULT 0"
    )
    await _ensure_column(
        "campaigns",
        "content",
        "ALTER TABLE campaigns ADD COLUMN content TEXT NOT NULL DEFAULT 'text'",
    )
    await _ensure_column(
        "campaigns", "saved_id", "ALTER TABLE campaigns ADD COLUMN saved_id INTEGER"
    )
    await _ensure_column(
        "users", "ref_by", "ALTER TABLE users ADD COLUMN ref_by INTEGER"
    )
    await _ensure_column(
        "invoices", "provider",
        "ALTER TABLE invoices ADD COLUMN provider TEXT NOT NULL DEFAULT 'platega'",
    )
    await _ensure_column(
        "campaigns", "pick",
        "ALTER TABLE campaigns ADD COLUMN pick TEXT NOT NULL DEFAULT 'random'",
    )
    await _ensure_column(
        "campaigns", "text_cursor",
        "ALTER TABLE campaigns ADD COLUMN text_cursor INTEGER NOT NULL DEFAULT 0",
    )


async def _ensure_column(table: str, column: str, ddl: str) -> None:
    async with _conn().execute(f"PRAGMA table_info({table})") as cursor:
        columns = {row["name"] for row in await cursor.fetchall()}
    if column in columns:
        return
    await _conn().execute(ddl)
    log.info("схема: в %s добавлена колонка %s", table, column)


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


#: Псевдоним: в карточке админки имя `subscription` занято ключом словаря.
subscription_of = None  # проставляется ниже, после определения subscription


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


async def profile(user_id: int) -> dict:
    """Профиль: монеты и сводка по рассылкам этого человека."""
    row = await _fetchone(
        """
        SELECT
            (SELECT coins FROM users WHERE user_id = ?) AS coins,
            (SELECT created_at FROM users WHERE user_id = ?) AS since,
            (SELECT COUNT(*) FROM accounts WHERE user_id = ?) AS accounts,
            (SELECT COUNT(*) FROM campaigns WHERE user_id = ?) AS campaigns,
            (SELECT COALESCE(SUM(sent_ok), 0) FROM campaigns
                 WHERE user_id = ?) AS sent,
            (SELECT COALESCE(SUM(stars), 0) FROM payments
                 WHERE user_id = ?) AS stars
        """,
        (user_id,) * 6,
    )
    return {key: int(row[key] or 0) for key in row.keys()} if row else {}


async def add_coins(user_id: int, coins: int, reason: str = "начисление") -> int:
    """Начислить монеты и записать это в журнал. Возвращает новый баланс."""
    await _conn().execute(
        "UPDATE users SET coins = coins + ? WHERE user_id = ?", (coins, user_id)
    )
    await _conn().execute(
        "INSERT INTO coin_ops (user_id, delta, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, coins, reason, int(time.time())),
    )
    await _conn().commit()
    row = await _fetchone("SELECT coins FROM users WHERE user_id = ?", (user_id,))
    return int(row["coins"]) if row else 0


async def spend_coins(user_id: int, coins: int, reason: str) -> bool:
    """Списать монеты. False — не хватило.

    Проверка и списание одним запросом, с условием на остаток. Не
    «прочитали, потом записали»: два одновременных запроса на покупку
    иначе оба увидели бы полный баланс и оба прошли — подписка за
    полцены.
    """
    cursor = await _conn().execute(
        "UPDATE users SET coins = coins - ? WHERE user_id = ? AND coins >= ?",
        (coins, user_id, coins),
    )
    if not cursor.rowcount:
        await _conn().rollback()
        return False
    await _conn().execute(
        "INSERT INTO coin_ops (user_id, delta, reason, created_at) "
        "VALUES (?, ?, ?, ?)",
        (user_id, -coins, reason, int(time.time())),
    )
    await _conn().commit()
    return True


async def coins_of(user_id: int) -> int:
    row = await _fetchone("SELECT coins FROM users WHERE user_id = ?", (user_id,))
    return int(row["coins"] or 0) if row else 0


async def coin_history(user_id: int, limit: int = 20) -> list[dict]:
    rows = await _fetchall(
        "SELECT delta, reason, created_at FROM coin_ops WHERE user_id = ? "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (user_id, limit),
    )
    return [
        {"delta": row["delta"], "reason": row["reason"],
         "created_at": row["created_at"]}
        for row in rows
    ]


# --- счета на оплату рублями ------------------------------------------


async def open_invoice(transaction_id: str, order_id: str, user_id: int,
                       coins: int, rub: float,
                       provider: str = "platega") -> None:
    """Записать выставленный счёт до того, как человек пойдёт платить."""
    await _conn().execute(
        """
        INSERT INTO invoices (transaction_id, order_id, user_id, coins, rub,
                              status, provider, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
        ON CONFLICT (transaction_id) DO NOTHING
        """,
        (transaction_id, order_id, user_id, coins, rub, provider,
         int(time.time())),
    )
    await _conn().commit()


async def invoice(transaction_id: str) -> dict | None:
    row = await _fetchone(
        "SELECT * FROM invoices WHERE transaction_id = ?", (transaction_id,)
    )
    return dict(row) if row else None


async def close_invoice(transaction_id: str, status: str) -> bool:
    """Закрыть счёт. False — он уже был закрыт.

    Условие на текущий статус стоит в самом запросе: Platega повторяет
    доставку callback, и два одновременных повтора иначе начислили бы
    монеты дважды.
    """
    cursor = await _conn().execute(
        "UPDATE invoices SET status = ?, closed_at = ? "
        "WHERE transaction_id = ? AND status = 'pending'",
        (status, int(time.time()), transaction_id),
    )
    await _conn().commit()
    return cursor.rowcount > 0


async def pending_invoices(max_age: int = 86400) -> list[dict]:
    rows = await _fetchall(
        "SELECT * FROM invoices WHERE status = 'pending' AND created_at > ? "
        "ORDER BY created_at",
        (int(time.time()) - max_age,),
    )
    return [dict(row) for row in rows]


# --- приглашения ------------------------------------------------------


async def set_referrer(user_id: int, ref_by: int) -> bool:
    """Записать пригласившего. False — не записали.

    Ставится ровно один раз и только тому, кто ещё никем не приглашён:
    иначе приглашённого можно было бы переприсвоить чужой ссылкой, а
    себя — пригласить самому.
    """
    if ref_by == user_id:
        return False
    exists = await _fetchone(
        "SELECT 1 FROM users WHERE user_id = ?", (ref_by,)
    )
    if exists is None:
        return False
    cursor = await _conn().execute(
        "UPDATE users SET ref_by = ? WHERE user_id = ? AND ref_by IS NULL",
        (ref_by, user_id),
    )
    await _conn().commit()
    return cursor.rowcount > 0


async def referrer_of(user_id: int) -> int | None:
    row = await _fetchone(
        "SELECT ref_by FROM users WHERE user_id = ?", (user_id,)
    )
    return int(row["ref_by"]) if row and row["ref_by"] else None


async def referral_stats(user_id: int) -> dict:
    row = await _fetchone(
        """
        SELECT
            (SELECT COUNT(*) FROM users WHERE ref_by = ?) AS invited,
            (SELECT COALESCE(SUM(delta), 0) FROM coin_ops
                 WHERE user_id = ? AND delta > 0
                   AND reason LIKE 'приглашённый%') AS earned
        """,
        (user_id, user_id),
    )
    return {"invited": int(row["invited"] or 0), "earned": int(row["earned"] or 0)}


async def record_payment(
    charge_id: str, user_id: int, stars: int, days: int, payload: str
) -> bool:
    """Записать оплату. False — такую уже записывали.

    Защита от двойного зачисления: Telegram присылает успешный платёж
    повторно, если бот не ответил вовремя, и без этой проверки один
    платёж продлевал бы подписку дважды.
    """
    try:
        await _conn().execute(
            "INSERT INTO payments (charge_id, user_id, stars, days, payload, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (charge_id, user_id, stars, days, payload, int(time.time())),
        )
    except aiosqlite.IntegrityError:
        log.info("платёж %s уже зачтён", charge_id)
        return False
    await _conn().commit()
    return True


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
    #: Ключи и параметры устройства этого аккаунта. None — общие из .env.
    api: dict | None = None
    #: phone | tdata
    source: str = "phone"
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
        api=_json(row["api"]) if "api" in row.keys() else None,
        source=(row["source"] if "source" in row.keys() else None) or "phone",
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
    api: dict | None = None,
    source: str = "phone",
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
                              session, status, added_at, checked_at,
                              api, source)
        VALUES (?, ?, ?, ?, ?, ?, 'ok', ?, ?, ?, ?)
        ON CONFLICT (user_id, phone) DO UPDATE SET
            tg_id      = excluded.tg_id,
            name       = excluded.name,
            username   = excluded.username,
            session    = excluded.session,
            status     = 'ok',
            checked_at = excluded.checked_at,
            api        = excluded.api,
            source     = excluded.source,
            note       = NULL
        """,
        (user_id, phone, tg_id, name, username, session, now, now,
         json.dumps(api) if api else None, source),
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


# --- чаты и папки аккаунта --------------------------------------------


@dataclass
class Chat:
    account_id: int
    #: Помеченный id, тот же, что показывает Telegram: у супергрупп и
    #: каналов он с приставкой -100.
    chat_id: int
    raw_id: int
    access_hash: int | None
    #: user | chat | channel — от этого зависит, каким InputPeer слать.
    kind: str
    #: Канал (в супергруппе флаг снят). На отправку не влияет, нужен,
    #: чтобы в списке чатов канал не назывался группой.
    broadcast: bool
    title: str
    username: str | None


def _chat(row: aiosqlite.Row) -> Chat:
    return Chat(
        account_id=row["account_id"],
        chat_id=row["chat_id"],
        raw_id=row["raw_id"],
        access_hash=row["access_hash"],
        kind=row["kind"],
        broadcast=bool(row["broadcast"]),
        title=row["title"] or str(row["chat_id"]),
        username=row["username"],
    )


async def save_chats(account_id: int, rows: list[dict]) -> int:
    """Переписать кэш диалогов аккаунта.

    Именно переписать: чаты, из которых аккаунт вышел, должны исчезнуть
    из списка, а не висеть в нём вечно. Всё одной транзакцией — иначе
    сканирование, оборвавшееся посередине, оставило бы человека с
    наполовину пустым списком.
    """
    now = int(time.time())
    await _conn().execute("BEGIN")
    try:
        await _conn().execute("DELETE FROM chats WHERE account_id = ?", (account_id,))
        await _conn().executemany(
            """
            INSERT INTO chats (account_id, chat_id, raw_id, access_hash,
                               kind, broadcast, title, username, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    account_id,
                    row["chat_id"],
                    row["raw_id"],
                    row.get("access_hash"),
                    row["kind"],
                    1 if row.get("broadcast") else 0,
                    row.get("title"),
                    row.get("username"),
                    now,
                )
                for row in rows
            ],
        )
    except Exception:
        await _conn().rollback()
        raise
    await _conn().commit()
    return len(rows)


async def chats(account_id: int) -> list[Chat]:
    rows = await _fetchall(
        "SELECT * FROM chats WHERE account_id = ? ORDER BY title", (account_id,)
    )
    return [_chat(row) for row in rows]


async def chats_by_ids(account_id: int, ids: list[int]) -> list[Chat]:
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    rows = await _fetchall(
        f"SELECT * FROM chats WHERE account_id = ? AND chat_id IN ({marks})",
        (account_id, *ids),
    )
    return [_chat(row) for row in rows]


@dataclass
class Folder:
    account_id: int
    folder_id: int
    title: str
    chat_ids: list[int]


async def save_folders(account_id: int, rows: list[dict]) -> int:
    now = int(time.time())
    await _conn().execute("BEGIN")
    try:
        await _conn().execute(
            "DELETE FROM folders WHERE account_id = ?", (account_id,)
        )
        await _conn().executemany(
            """
            INSERT INTO folders (account_id, folder_id, title, chat_ids, scanned_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    account_id,
                    row["folder_id"],
                    row.get("title"),
                    json.dumps(row.get("chat_ids") or []),
                    now,
                )
                for row in rows
            ],
        )
    except Exception:
        await _conn().rollback()
        raise
    await _conn().commit()
    return len(rows)


async def folders(account_id: int) -> list[Folder]:
    rows = await _fetchall(
        "SELECT * FROM folders WHERE account_id = ? ORDER BY folder_id", (account_id,)
    )
    out = []
    for row in rows:
        try:
            ids = json.loads(row["chat_ids"])
        except (json.JSONDecodeError, TypeError):
            ids = []
        out.append(
            Folder(
                account_id=row["account_id"],
                folder_id=row["folder_id"],
                title=row["title"] or f"Папка {row['folder_id']}",
                chat_ids=[int(i) for i in ids],
            )
        )
    return out


async def folder(account_id: int, folder_id: int) -> Folder | None:
    found = [f for f in await folders(account_id) if f.folder_id == folder_id]
    return found[0] if found else None


# --- рассылки ---------------------------------------------------------


@dataclass
class Campaign:
    id: int
    user_id: int
    account_id: int
    title: str
    text: str
    #: Пауза между сообщениями, секунды. Круг по всем чатам занимает
    #: interval × количество чатов — это стоит помнить, читая «раз в час».
    interval: int
    source: str
    folder_id: int | None
    #: text — писать текстом из поля text; saved — копировать сообщение
    #: из «Избранного» аккаунта вместе с медиа и оформлением.
    content: str
    saved_id: int | None
    #: order — варианты по очереди, random — вразнобой.
    pick: str
    text_cursor: int
    status: str
    cursor: int
    next_run_at: int
    sent_ok: int
    sent_err: int
    cycles: int
    note: str | None
    created_at: int


def _campaign(row: aiosqlite.Row) -> Campaign:
    return Campaign(
        id=row["id"],
        user_id=row["user_id"],
        account_id=row["account_id"],
        title=row["title"] or "Рассылка",
        text=row["text"],
        interval=row["interval"],
        source=row["source"],
        folder_id=row["folder_id"],
        content=(row["content"] if "content" in row.keys() else None) or "text",
        saved_id=row["saved_id"] if "saved_id" in row.keys() else None,
        pick=(row["pick"] if "pick" in row.keys() else None) or "random",
        text_cursor=row["text_cursor"] if "text_cursor" in row.keys() else 0,
        status=row["status"],
        cursor=row["cursor"],
        next_run_at=row["next_run_at"],
        sent_ok=row["sent_ok"],
        sent_err=row["sent_err"],
        cycles=row["cycles"],
        note=row["note"],
        created_at=row["created_at"],
    )


async def create_campaign(
    user_id: int,
    account_id: int,
    *,
    title: str,
    text: str,
    interval: int,
    source: str,
    folder_id: int | None,
    chat_ids: list[int],
    start_at: int,
    content: str = "text",
    saved_id: int | None = None,
    pick: str = "random",
) -> int:
    now = int(time.time())
    cursor = await _conn().execute(
        """
        INSERT INTO campaigns (user_id, account_id, title, text, interval,
                               source, folder_id, content, saved_id, pick,
                               status, next_run_at, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)
        """,
        (user_id, account_id, title, text, interval, source, folder_id,
         content, saved_id, pick, start_at, now),
    )
    campaign_id = cursor.lastrowid
    # Порядок чатов фиксируется здесь и дальше не меняется: «по очереди»
    # должно означать один и тот же круг, а не случайную выборку при
    # каждом запуске.
    await _conn().executemany(
        "INSERT INTO campaign_targets (campaign_id, position, chat_id) "
        "VALUES (?, ?, ?)",
        [(campaign_id, position, chat_id)
         for position, chat_id in enumerate(chat_ids)],
    )
    await _conn().commit()
    return int(campaign_id)


async def set_variants(campaign_id: int, variants: list[dict]) -> None:
    """Переписать варианты сообщения одной рассылки."""
    await _conn().execute(
        "DELETE FROM campaign_texts WHERE campaign_id = ?", (campaign_id,)
    )
    await _conn().executemany(
        "INSERT INTO campaign_texts (campaign_id, position, content, text, "
        "saved_id) VALUES (?, ?, ?, ?, ?)",
        [
            (campaign_id, position, item.get("content") or "text",
             item.get("text") or "", item.get("saved_id"))
            for position, item in enumerate(variants)
        ],
    )
    await _conn().commit()


async def variants(campaign: Campaign) -> list[dict]:
    """Варианты сообщения. Пусто — берём то, что лежит в самой рассылке.

    Запасной путь нужен старым рассылкам: они заведены до появления
    вариантов, и их единственное сообщение живёт в полях campaigns.
    Переливать его отдельной миграцией незачем — достаточно прочитать
    оттуда.
    """
    rows = await _fetchall(
        "SELECT content, text, saved_id FROM campaign_texts "
        "WHERE campaign_id = ? ORDER BY position",
        (campaign.id,),
    )
    if rows:
        return [
            {"content": row["content"], "text": row["text"] or "",
             "saved_id": row["saved_id"]}
            for row in rows
        ]
    return [{
        "content": campaign.content,
        "text": campaign.text,
        "saved_id": campaign.saved_id,
    }]


async def set_text_cursor(campaign_id: int, cursor: int) -> None:
    await _conn().execute(
        "UPDATE campaigns SET text_cursor = ? WHERE id = ?",
        (cursor, campaign_id),
    )
    await _conn().commit()


# --- подписка на чаты -------------------------------------------------


async def log_join(account_id: int, chat_id: int, ok: bool,
                   error: str | None) -> None:
    await _conn().execute(
        "INSERT INTO joins (account_id, chat_id, ok, error, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (account_id, chat_id, 1 if ok else 0, error, int(time.time())),
    )
    await _conn().commit()


async def joins_today(account_id: int) -> int:
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM joins WHERE account_id = ? AND created_at > ?",
        (account_id, int(time.time()) - 86400),
    )
    return int(row["n"]) if row else 0


async def join_tried(account_id: int, chat_id: int, within: int) -> bool:
    """Пробовали ли уже вступить в этот чат недавно.

    Без этой проверки движок ломился бы в закрытый чат каждый круг, а
    вступления Telegram считает отдельно от сообщений и наказывает за
    них так же.
    """
    row = await _fetchone(
        "SELECT 1 FROM joins WHERE account_id = ? AND chat_id = ? "
        "AND created_at > ? LIMIT 1",
        (account_id, chat_id, int(time.time()) - within),
    )
    return row is not None


async def campaigns(user_id: int) -> list[Campaign]:
    rows = await _fetchall(
        "SELECT * FROM campaigns WHERE user_id = ? ORDER BY created_at DESC",
        (user_id,),
    )
    return [_campaign(row) for row in rows]


async def campaign(user_id: int, campaign_id: int) -> Campaign | None:
    row = await _fetchone(
        "SELECT * FROM campaigns WHERE id = ? AND user_id = ?",
        (campaign_id, user_id),
    )
    return _campaign(row) if row else None


async def due_campaigns(now: int) -> list[Campaign]:
    rows = await _fetchall(
        "SELECT * FROM campaigns WHERE status = 'running' AND next_run_at <= ? "
        "ORDER BY next_run_at",
        (now,),
    )
    return [_campaign(row) for row in rows]


async def campaign_targets(campaign_id: int) -> list[int]:
    rows = await _fetchall(
        "SELECT chat_id FROM campaign_targets WHERE campaign_id = ? "
        "ORDER BY position",
        (campaign_id,),
    )
    return [int(row["chat_id"]) for row in rows]


async def set_campaign_status(
    campaign_id: int, status: str, note: str | None = None
) -> None:
    await _conn().execute(
        "UPDATE campaigns SET status = ?, note = ? WHERE id = ?",
        (status, note, campaign_id),
    )
    await _conn().commit()


async def reschedule_campaign(campaign_id: int, next_run_at: int) -> None:
    await _conn().execute(
        "UPDATE campaigns SET next_run_at = ? WHERE id = ?",
        (next_run_at, campaign_id),
    )
    await _conn().commit()


async def advance_campaign(
    campaign_id: int, *, cursor: int, next_run_at: int, cycles: int, ok: bool
) -> None:
    """Сдвинуть круг после отправки и записать её итог.

    Заодно снимается note: там висят жалобы вида «упёрлись в дневной
    лимит», и после состоявшейся отправки они уже неправда.
    """
    await _conn().execute(
        f"""
        UPDATE campaigns
           SET cursor = ?, next_run_at = ?, cycles = ?, note = NULL,
               {'sent_ok = sent_ok + 1' if ok else 'sent_err = sent_err + 1'}
         WHERE id = ?
        """,
        (cursor, next_run_at, cycles, campaign_id),
    )
    await _conn().commit()


async def edit_campaign(
    user_id: int,
    campaign_id: int,
    *,
    title: str,
    text: str,
    interval: int,
    content: str,
    saved_id: int | None,
    pick: str = "random",
) -> bool:
    """Поменять текст, материал, интервал и название.

    Круг и счётчики не сбрасываются намеренно: поправить опечатку в
    тексте — не повод начинать обход чатов заново и писать в те, куда
    уже написали.
    """
    cursor = await _conn().execute(
        """
        UPDATE campaigns
           SET title = ?, text = ?, interval = ?, content = ?, saved_id = ?,
               pick = ?
         WHERE id = ? AND user_id = ?
        """,
        (title, text, interval, content, saved_id, pick, campaign_id, user_id),
    )
    await _conn().commit()
    return cursor.rowcount > 0


async def delete_campaign(user_id: int, campaign_id: int) -> bool:
    cursor = await _conn().execute(
        "DELETE FROM campaigns WHERE id = ? AND user_id = ?", (campaign_id, user_id)
    )
    if cursor.rowcount:
        await _conn().execute(
            "DELETE FROM campaign_targets WHERE campaign_id = ?", (campaign_id,)
        )
        await _conn().execute(
            "DELETE FROM sends WHERE campaign_id = ?", (campaign_id,)
        )
        await _conn().execute(
            "DELETE FROM campaign_texts WHERE campaign_id = ?", (campaign_id,)
        )
    await _conn().commit()
    return cursor.rowcount > 0


async def stop_account_campaigns(account_id: int, note: str) -> int:
    """Остановить все рассылки аккаунта — при PeerFlood и подобном."""
    cursor = await _conn().execute(
        "UPDATE campaigns SET status = 'stopped', note = ? "
        "WHERE account_id = ? AND status = 'running'",
        (note, account_id),
    )
    await _conn().commit()
    return cursor.rowcount


# --- материалы из «Избранного» ----------------------------------------


async def save_materials(account_id: int, rows: list[dict]) -> int:
    """Переписать список материалов аккаунта.

    Переписать, а не дописать: сообщение могли удалить из «Избранного»,
    и висеть в списке оно не должно — отправка по нему всё равно
    провалится.
    """
    now = int(time.time())
    await _conn().execute("BEGIN")
    try:
        await _conn().execute(
            "DELETE FROM materials WHERE account_id = ?", (account_id,)
        )
        await _conn().executemany(
            """
            INSERT INTO materials (account_id, msg_id, kind, preview,
                                   has_media, scanned_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                (account_id, row["msg_id"], row["kind"], row.get("preview"),
                 1 if row.get("has_media") else 0, now)
                for row in rows
            ],
        )
    except Exception:
        await _conn().rollback()
        raise
    await _conn().commit()
    return len(rows)


async def materials(account_id: int) -> list[dict]:
    rows = await _fetchall(
        "SELECT * FROM materials WHERE account_id = ? ORDER BY msg_id DESC",
        (account_id,),
    )
    return [
        {
            "msg_id": row["msg_id"],
            "kind": row["kind"],
            "preview": row["preview"],
            "has_media": bool(row["has_media"]),
        }
        for row in rows
    ]


async def material(account_id: int, msg_id: int) -> dict | None:
    found = [m for m in await materials(account_id) if m["msg_id"] == msg_id]
    return found[0] if found else None


# --- журнал отправок и лимиты -----------------------------------------


async def log_send(
    campaign_id: int,
    account_id: int,
    chat_id: int,
    title: str | None,
    ok: bool,
    error: str | None,
) -> None:
    await _conn().execute(
        """
        INSERT INTO sends (campaign_id, account_id, chat_id, title, ok,
                           error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (campaign_id, account_id, chat_id, title, 1 if ok else 0, error,
         int(time.time())),
    )
    await _conn().commit()


async def sends(campaign_id: int, limit: int = 30) -> list[dict]:
    rows = await _fetchall(
        "SELECT * FROM sends WHERE campaign_id = ? ORDER BY created_at DESC "
        "LIMIT ?",
        (campaign_id, limit),
    )
    return [
        {
            "chat_id": row["chat_id"],
            "title": row["title"],
            "ok": bool(row["ok"]),
            "error": row["error"],
            "created_at": row["created_at"],
        }
        for row in rows
    ]


async def sent_today(account_id: int) -> int:
    """Сколько сообщений аккаунт отправил за сутки.

    Считается по журналу, а не отдельным счётчиком: счётчик может
    разъехаться с реальностью, сумма по журналу — нет.
    """
    row = await _fetchone(
        "SELECT COUNT(*) AS n FROM sends "
        "WHERE account_id = ? AND ok = 1 AND created_at > ?",
        (account_id, int(time.time()) - 86400),
    )
    return int(row["n"]) if row else 0


async def last_send_at(account_id: int) -> int:
    """Когда аккаунт писал в последний раз — для паузы между сообщениями.

    Пауза общая на аккаунт, а не на рассылку: Telegram смотрит на
    аккаунт, и три рассылки с интервалом в минуту — это для него один
    аккаунт, пишущий втрое чаще.
    """
    row = await _fetchone(
        "SELECT MAX(created_at) AS last FROM sends WHERE account_id = ?",
        (account_id,),
    )
    return int(row["last"] or 0) if row else 0


async def pause_account(account_id: int, until: int, reason: str) -> None:
    await _conn().execute(
        "INSERT INTO account_pauses (account_id, until, reason) VALUES (?, ?, ?) "
        "ON CONFLICT (account_id) DO UPDATE SET until = excluded.until, "
        "reason = excluded.reason",
        (account_id, until, reason),
    )
    await _conn().commit()


async def account_pause(account_id: int) -> tuple[int, str] | None:
    """До какого времени аккаунт молчит. None — не молчит."""
    row = await _fetchone(
        "SELECT until, reason FROM account_pauses WHERE account_id = ?",
        (account_id,),
    )
    if row is None or int(row["until"]) <= int(time.time()):
        return None
    return int(row["until"]), row["reason"] or ""


# --- настройки из бота ------------------------------------------------


async def setting(key: str, default: str = "") -> str:
    row = await _fetchone("SELECT value FROM settings WHERE key = ?", (key,))
    return (row["value"] if row and row["value"] is not None else default)


async def set_setting(key: str, value: str | None) -> None:
    """Записать настройку. None стирает её и возвращает значение по
    умолчанию — так «сбросить» не требует отдельной таблицы флагов."""
    if value is None:
        await _conn().execute("DELETE FROM settings WHERE key = ?", (key,))
    else:
        await _conn().execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (key) DO UPDATE SET value = excluded.value, "
            "updated_at = excluded.updated_at",
            (key, value, int(time.time())),
        )
    await _conn().commit()


async def settings_all() -> dict:
    rows = await _fetchall("SELECT key, value FROM settings", ())
    return {row["key"]: row["value"] for row in rows}


# --- админка: разбор обращений ----------------------------------------


async def get_user_exists(user_id: int) -> bool:
    row = await _fetchone("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    return row is not None


async def find_users(query: str, limit: int = 10) -> list[dict]:
    """Найти людей по id или нику. Для разбора обращений в поддержку."""
    query = (query or "").strip().lstrip("@")
    if not query:
        return []
    if query.isdigit():
        rows = await _fetchall(
            "SELECT * FROM users WHERE user_id = ?", (int(query),)
        )
    else:
        rows = await _fetchall(
            "SELECT * FROM users WHERE username LIKE ? ORDER BY seen_at DESC "
            "LIMIT ?",
            (f"%{query}%", limit),
        )
    return [dict(row) for row in rows]


async def user_card(user_id: int) -> dict | None:
    """Всё про человека одним куском: чем помогать, видно сразу."""
    row = await _fetchone("SELECT * FROM users WHERE user_id = ?", (user_id,))
    if row is None:
        return None
    subscription = await subscription_of(user_id)
    return {
        "user": dict(row),
        "subscription": {
            "kind": subscription.kind,
            "until": subscription.until,
            "days_left": subscription.days_left,
        },
        "accounts": [
            {"id": a.id, "phone": a.phone, "name": a.title,
             "status": a.status, "note": a.note, "source": a.source}
            for a in await accounts(user_id)
        ],
        "campaigns": [
            {"id": c.id, "title": c.title, "status": c.status,
             "sent_ok": c.sent_ok, "sent_err": c.sent_err, "note": c.note}
            for c in await campaigns(user_id)
        ],
        "coins": await coin_history(user_id, 10),
        "payments": [
            dict(r) for r in await _fetchall(
                "SELECT charge_id, stars, days, payload, created_at FROM payments "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
                (user_id,),
            )
        ],
        "invoices": [
            dict(r) for r in await _fetchall(
                "SELECT transaction_id, coins, rub, status, created_at "
                "FROM invoices WHERE user_id = ? ORDER BY created_at DESC LIMIT 10",
                (user_id,),
            )
        ],
        "sends": [
            dict(r) for r in await _fetchall(
                "SELECT s.title, s.ok, s.error, s.created_at FROM sends s "
                "JOIN campaigns c ON c.id = s.campaign_id "
                "WHERE c.user_id = ? ORDER BY s.created_at DESC LIMIT 15",
                (user_id,),
            )
        ],
    }


async def set_campaign_status_admin(campaign_id: int, status: str) -> bool:
    """Остановить или продолжить чужую рассылку — из админки."""
    cursor = await _conn().execute(
        "UPDATE campaigns SET status = ?, next_run_at = ? WHERE id = ?",
        (status, int(time.time()), campaign_id),
    )
    await _conn().commit()
    return cursor.rowcount > 0


# --- статистика для админа --------------------------------------------


async def all_recipients() -> list[dict]:
    """Кому уходит рассылка владельца — всем, кто когда-либо жал /start.

    Отсортировано по последнему появлению: если рассылка упрётся в
    лимиты Telegram и её придётся оборвать, первыми получат сообщение
    те, кто заходил недавно.

    Имя и ник берём здесь же: в рассылке они нужны для подстановки, а
    отдельный запрос на каждого получателя — это лишняя тысяча запросов
    к базе на ровном месте.
    """
    rows = await _fetchall(
        "SELECT user_id, username, name FROM users ORDER BY seen_at DESC"
    )
    return [dict(row) for row in rows]


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


def _json(raw):
    if not raw:
        return None
    try:
        found = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return found if isinstance(found, dict) else None


async def _fetchone(sql: str, args: tuple = ()) -> aiosqlite.Row | None:
    async with _conn().execute(sql, args) as cursor:
        return await cursor.fetchone()


async def _fetchall(sql: str, args: tuple = ()) -> list[aiosqlite.Row]:
    async with _conn().execute(sql, args) as cursor:
        return list(await cursor.fetchall())


# Псевдоним для админки: см. комментарий выше.
subscription_of = subscription
