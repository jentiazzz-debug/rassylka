"""Настройки: бот, мини-апп, ключи MTProto, шифрование сессий.

Всё читается из .env рядом с main.py. Общий .env в корне папки ботов
намеренно не подхватывается: там лежит токен другого бота, а поллинг с
одним токеном может вести только один процесс.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("rassylka.config")

#: Хостинги отдают под данные отдельный том и сообщают о нём переменной
#: DATA_DIR. Не слушать её нельзя: база ляжет рядом с кодом и сотрётся на
#: первом же передеплое — вместе со строками сессий, то есть человеку
#: придётся заново подключать все аккаунты по номеру и коду.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

WEBAPP_DIR = BASE_DIR / "webapp"


def _int(name: str, default: str) -> int:
    raw = (os.getenv(name) or default).strip()
    return int(raw) if raw.lstrip("-").isdigit() else int(default)


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _ints(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return out


# --- бот --------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("DB_PATH") or DATA_DIR / "rassylka.db")

#: ADMIN_ID и OWNER_ID — синонимы: так называется одно и то же в разных
#: ботах этой папки, и путать их при переносе .env не хочется.
ADMIN_IDS = (
    _ints(os.getenv("ADMIN_IDS"))
    | _ints(os.getenv("ADMIN_ID"))
    | _ints(os.getenv("OWNER_ID"))
)


def _support_url() -> str:
    """Ссылка на поддержку: из готового адреса либо из ника."""
    raw = (os.getenv("SUPPORT_URL") or "").strip()
    if raw:
        return raw
    nick = (os.getenv("SUPPORT_USERNAME") or "").strip().lstrip("@")
    return f"https://t.me/{nick}" if nick else ""


SUPPORT_URL = _support_url()


# --- мини-апп ---------------------------------------------------------

#: Публичный адрес мини-аппа. Telegram открывает WebApp только по https
#: и только по адресу, который знает клиент, — localhost в кнопку не
#: положить. На своей машине это туннель (cloudflared / ngrok), на
#: хостинге — домен. Пусто — кнопка «Открыть приложение» не появится,
#: и бот скажет об этом в лог при старте.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")

#: Свой веб-сервер: он же раздаёт мини-апп, он же отвечает на его
#: запросы. Отдельный процесс не нужен — Telethon-клиенты незавершённых
#: входов живут в памяти, и разносить их с API по разным процессам нельзя.
HOST = (os.getenv("HOST") or "0.0.0.0").strip()
PORT = _int("PORT", "8080")

#: Сколько секунд initData считается свежей. Telegram подписывает её при
#: открытии приложения, и подпись не истекает сама — без своей проверки
#: один раз подсмотренная строка работала бы как вечный пароль.
INITDATA_TTL = _int("INITDATA_TTL", "86400")


# --- аккаунты для рассылки --------------------------------------------

MTPROTO_API_ID = _int("MTPROTO_API_ID", "0")
MTPROTO_API_HASH = (os.getenv("MTPROTO_API_HASH") or "").strip()

#: Сколько аккаунтов может подключить один человек. Несколько номеров —
#: это не про объём в одного, а про то, что лимиты Telegram выдаёт
#: персонально: пока один ждёт FloodWait, работает следующий.
MAX_ACCOUNTS = max(1, _int("MAX_ACCOUNTS", "5"))

#: Сколько живёт незавершённый вход. Между «прислать код» и «ввести код»
#: в памяти висит подключённый Telethon-клиент: phone_code_hash привязан
#: к его ключу авторизации, и на новом подключении тот же код не примут.
#: Долго держать такие клиенты не нужно — код всё равно протухает у
#: Telegram за несколько минут.
LOGIN_TTL = _int("LOGIN_TTL", "600")

#: Сколько раз в час один человек может просить код. Каждый запрос
#: Telegram считает попыткой входа и на частых выдаёт FloodWait на часы —
#: уже не боту, а номеру человека. Поэтому тормоз стоит у нас, до
#: обращения к Telegram.
CODE_REQUESTS_PER_HOUR = max(1, _int("CODE_REQUESTS_PER_HOUR", "5"))

#: Ключ шифрования строк сессий (Fernet, base64). Строка сессии — это
#: полный доступ к аккаунту: с ней читают переписку и пишут от его имени.
#: В базе она лежит только зашифрованной.
#:
#: Своего ключа в .env нет — генерируем и кладём файлом рядом с базой.
#: Ключ и база в одном томе означают, что доступ к тому даёт и то и
#: другое: на хостинге ключ лучше держать в переменной окружения, а файл
#: оставить как режим «запустил и работает».
SESSION_KEY = (os.getenv("SESSION_KEY") or "").strip()
SESSION_KEY_FILE = Path(os.getenv("SESSION_KEY_FILE") or DATA_DIR / "session.key")


# --- подписка ---------------------------------------------------------

#: Бесплатный пробный период, дней. Считается от первого /start.
TRIAL_DAYS = max(0, _int("TRIAL_DAYS", "5"))

#: Подпись, которая дописывается в конец каждого сообщения рассылки на
#: бесплатном тарифе. На платном — не дописывается. Сама рассылка будет
#: дальше, но текст подписи живёт здесь с самого начала: он часть
#: договорённости с человеком, а не деталь реализации отправки.
FREE_FOOTER = (os.getenv("FREE_FOOTER") or "").strip()


def webapp_ready() -> bool:
    """Есть ли куда открывать приложение.

    Telegram кладёт в кнопку только https-адрес: http и localhost клиент
    молча не примет — кнопка будет, а нажатие ничего не даст.
    """
    return WEBAPP_URL.startswith("https://")


def mtproto_ready() -> bool:
    return bool(MTPROTO_API_ID and MTPROTO_API_HASH)


def check() -> None:
    """Что не так с настройками. Молчит, когда всё на месте."""
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN пуст. Впишите в .env токен от @BotFather "
            "(шаблон — в .env.example)."
        )
    if not webapp_ready():
        log.warning(
            "WEBAPP_URL %s — кнопки «Открыть приложение» не будет. "
            "Telegram открывает мини-апп только по https; на своей "
            "машине поднимите туннель (cloudflared tunnel --url "
            "http://localhost:%s) и впишите его адрес.",
            f"= {WEBAPP_URL!r}" if WEBAPP_URL else "не задан",
            PORT,
        )
    if not mtproto_ready():
        log.warning(
            "MTPROTO_API_ID / MTPROTO_API_HASH не заданы — подключить "
            "аккаунт по номеру не выйдет. Ключи выдают бесплатно на "
            "my.telegram.org, раздел API development tools."
        )
    if not SUPPORT_URL:
        log.warning(
            "SUPPORT_USERNAME не задан — кнопки «Поддержка» в меню не будет."
        )
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не заданы: /stats не откроется ни у кого")


def session_key() -> bytes:
    """Ключ шифрования сессий: из окружения либо из файла рядом с базой.

    Файл создаётся один раз. Потерять его — то же, что потерять все
    подключённые аккаунты: расшифровать сессии будет нечем, и людям
    придётся входить по номеру заново.
    """
    if SESSION_KEY:
        return SESSION_KEY.encode()
    if SESSION_KEY_FILE.exists():
        saved = SESSION_KEY_FILE.read_bytes().strip()
        if saved:
            return saved

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    SESSION_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_KEY_FILE.write_bytes(key)
    try:
        SESSION_KEY_FILE.chmod(0o600)
    except OSError:
        # Windows про режимы файлов не знает — не повод падать.
        pass
    log.warning(
        "создан новый ключ шифрования сессий: %s. Не удаляйте его и не "
        "теряйте при переезде — иначе все подключённые аккаунты придётся "
        "подключать заново.",
        SESSION_KEY_FILE,
    )
    return key


def new_token() -> str:
    """Одноразовый идентификатор незавершённого входа."""
    return secrets.token_urlsafe(18)
