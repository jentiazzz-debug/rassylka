"""Веб-слой мини-аппа: раздача страницы и API к ней.

Живёт в том же процессе, что и бот, — иначе незавершённый вход по номеру
работать не может: phone_code_hash привязан к живому подключению
Telethon, а оно висит в памяти (см. accounts.py).

Кто спрашивает — проверяется по initData. Telegram отдаёт мини-аппу
подписанную строку с данными о человеке, и подпись эту умеет проверить
только тот, у кого есть токен бота. Без такой проверки любой мог бы
дёрнуть наш API с чужим user_id и подключить аккаунт на чужое имя или
увидеть чужие номера. Поэтому user_id берётся ровно из проверенной
initData и никогда — из тела запроса.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import urllib.parse
from functools import wraps

from aiohttp import web

import accounts
import config
import db

log = logging.getLogger("rassylka.webapp")

#: Заголовок, в котором мини-апп присылает initData. Тело запроса тоже
#: читается — на случай, если заголовок срежет прокси хостинга.
INIT_HEADER = "X-Init-Data"


# --- проверка initData ------------------------------------------------


def _check_string(fields: dict[str, str], drop: set[str]) -> str:
    return "\n".join(
        f"{key}={value}" for key, value in sorted(fields.items()) if key not in drop
    )


def parse_init_data(raw: str) -> dict | None:
    """Разобрать и проверить initData. None — подпись не сошлась.

    Проверяется подписью на токене бота: HMAC-ключ выводится из токена с
    солью «WebAppData», и повторить такую подпись без токена нельзя.

    Полем `signature` Telegram подписывает те же данные ещё и своим
    ключом — для сторонних сервисов, у которых токена нет. В строку для
    HMAC оно то входит, то нет: набор полей у Telegram со временем
    менялся. Поэтому считаем оба варианта и принимаем любой сошедшийся —
    подделать всё равно нужен токен бота, так что вариантность здесь
    ничего не ослабляет, а неучтённое поле иначе ломало бы вход всем.
    """
    if not raw or not config.BOT_TOKEN:
        return None
    try:
        fields = dict(urllib.parse.parse_qsl(raw, keep_blank_values=True))
    except ValueError:
        return None

    got = fields.pop("hash", "")
    if not got:
        return None

    secret = hmac.new(b"WebAppData", config.BOT_TOKEN.encode(), hashlib.sha256).digest()
    for drop in ({"hash"}, {"hash", "signature"}):
        expected = hmac.new(
            secret, _check_string(fields, drop).encode(), hashlib.sha256
        ).hexdigest()
        if hmac.compare_digest(expected, got):
            break
    else:
        return None

    # Свежесть. Подпись сама не истекает: без этой проверки один раз
    # подсмотренная строка работала бы как вечный пароль.
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError:
        return None
    if config.INITDATA_TTL and time.time() - auth_date > config.INITDATA_TTL:
        return None

    try:
        user = json.loads(fields.get("user") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(user, dict) or not user.get("id"):
        return None
    return user


async def _body(request: web.Request) -> dict:
    try:
        data = await request.json()
    except (json.JSONDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def authed(handler):
    """Обёртка: проверить initData и передать хендлеру данные человека."""

    @wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        data = await _body(request)
        raw = request.headers.get(INIT_HEADER) or data.get("init_data") or ""
        user = parse_init_data(raw)
        if user is None:
            log.info("отказ по подписи: %s", request.path)
            return web.json_response(
                {"ok": False, "error": "Откройте приложение из бота заново."},
                status=401,
            )
        return await handler(request, user, data)

    return wrapper


# --- ответы -----------------------------------------------------------


def _fail(error: str, **extra) -> web.Response:
    """Ошибка, которую показываем человеку.

    Статус 200: это не сбой протокола, а обычный ответ «так нельзя», и
    во фронтенде он разбирается тем же кодом, что и удача. Настоящие
    ошибки (подпись, битый запрос) отдаются своими кодами.
    """
    return web.json_response({"ok": False, "error": error, **extra})


def _account_view(account: db.Account) -> dict:
    """Аккаунт для показа в приложении. Без сессии — она не уходит никуда."""
    return {
        "id": account.id,
        "phone": account.phone,
        "name": account.title,
        "username": account.username,
        "status": account.status,
        "note": account.note,
        "added_at": account.added_at,
        "checked_at": account.checked_at,
    }


async def _state(user_id: int) -> dict:
    subscription = await db.subscription(user_id)
    return {
        "ok": True,
        "subscription": {
            "kind": subscription.kind,
            "until": subscription.until,
            "days_left": subscription.days_left,
            "active": subscription.active,
            "paid": subscription.paid,
        },
        "accounts": [_account_view(a) for a in await db.accounts(user_id)],
        "limits": {
            "max_accounts": config.MAX_ACCOUNTS,
            "trial_days": config.TRIAL_DAYS,
        },
        "support_url": config.SUPPORT_URL,
        "mtproto_ready": config.mtproto_ready(),
    }


# --- ручки ------------------------------------------------------------


@authed
async def api_state(request: web.Request, user: dict, data: dict) -> web.Response:
    """Всё, что нужно приложению при открытии."""
    user_id = int(user["id"])
    # Открыть приложение можно и не нажав /start в боте (по прямой
    # ссылке): человека надо завести, иначе у него не будет ни записи,
    # ни пробного периода.
    await db.ensure_user(
        user_id,
        user.get("username"),
        " ".join(
            part for part in (user.get("first_name"), user.get("last_name")) if part
        )
        or None,
    )
    state = await _state(user_id)
    # Незавершённый вход переживает закрытие приложения: свернул, пошёл
    # за кодом в чат «Telegram», вернулся — и попадает на тот же шаг, а
    # не на пустую форму с номером.
    pending = accounts.pending_for(user_id)
    if pending is not None:
        state["pending"] = {
            "token": pending.token,
            "phone": pending.phone,
            "stage": pending.stage,
        }
    return web.json_response(state)


@authed
async def api_login_start(request: web.Request, user: dict, data: dict) -> web.Response:
    user_id = int(user["id"])
    await db.ensure_user(user_id, user.get("username"), None)
    try:
        pending = await accounts.start(user_id, str(data.get("phone") or ""))
    except accounts.LoginError as error:
        return _fail(error.message, retry_after=error.retry_after)
    return web.json_response(
        {
            "ok": True,
            "token": pending.token,
            "phone": pending.phone,
            "stage": "code",
        }
    )


@authed
async def api_login_code(request: web.Request, user: dict, data: dict) -> web.Response:
    token = str(data.get("token") or "")
    if not _owns(token, user):
        return _fail("Вход уже не активен. Начните заново.", restart=True)
    try:
        account = await accounts.submit_code(token, str(data.get("code") or ""))
    except accounts.LoginError as error:
        return _fail(error.message, restart=error.restart, retry_after=error.retry_after)
    if account is None:
        return web.json_response({"ok": True, "stage": "password"})
    return web.json_response(
        {"ok": True, "stage": "done", "account": _account_view(account)}
    )


@authed
async def api_login_password(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    token = str(data.get("token") or "")
    if not _owns(token, user):
        return _fail("Вход уже не активен. Начните заново.", restart=True)
    try:
        # Пароль дальше Telegram не уходит: он не пишется ни в базу, ни в
        # лог — и в этой функции нигде не печатается.
        account = await accounts.submit_password(
            token, str(data.get("password") or "")
        )
    except accounts.LoginError as error:
        return _fail(error.message, restart=error.restart, retry_after=error.retry_after)
    return web.json_response(
        {"ok": True, "stage": "done", "account": _account_view(account)}
    )


@authed
async def api_login_cancel(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    token = str(data.get("token") or "")
    if _owns(token, user):
        await accounts.cancel(token)
    return web.json_response({"ok": True})


@authed
async def api_account_verify(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    account_id = _int(data.get("id"))
    account = await accounts.verify(int(user["id"]), account_id)
    if account is None:
        return _fail("Такого аккаунта нет.")
    return web.json_response({"ok": True, "account": _account_view(account)})


@authed
async def api_account_forget(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    account_id = _int(data.get("id"))
    if not await accounts.forget(int(user["id"]), account_id):
        return _fail("Такого аккаунта нет.")
    return web.json_response({"ok": True})


def _owns(token: str, user: dict) -> bool:
    """Тот ли это человек, который начинал вход.

    Токен незавершённого входа — случайная строка, но проверять
    принадлежность всё равно надо: без этого угаданный токен позволил бы
    завершить чужой вход и привязать чужой аккаунт к себе.
    """
    return accounts.owner_of(token) == int(user["id"])


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


# --- страница ---------------------------------------------------------


async def page(request: web.Request) -> web.Response:
    """Сама страница мини-аппа.

    Без кэша: приложение открывается внутри Telegram, и его WebView
    охотно показывает вчерашнюю версию — после правки фронтенда человек
    видел бы старый экран и не понимал, почему ничего не изменилось.
    """
    index = config.WEBAPP_DIR / "index.html"
    if not index.exists():
        return web.Response(text="webapp/index.html не найден", status=500)
    return web.Response(
        text=index.read_text(encoding="utf-8"),
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def asset(request: web.Request) -> web.Response:
    """Стили и скрипт. Список файлов задан явно — каталог наружу не отдаём."""
    allowed = {"app.js": "application/javascript", "app.css": "text/css"}
    name = request.match_info.get("name", "")
    kind = allowed.get(name)
    if kind is None:
        raise web.HTTPNotFound()
    path = config.WEBAPP_DIR / name
    if not path.exists():
        raise web.HTTPNotFound()
    return web.Response(
        text=path.read_text(encoding="utf-8"),
        content_type=kind,
        charset="utf-8",
        headers={"Cache-Control": "no-store"},
    )


async def health(request: web.Request) -> web.Response:
    """Пинг для хостинга: жив ли процесс."""
    return web.json_response({"ok": True, "logins_pending": accounts.pending_count()})


def build() -> web.Application:
    app = web.Application()
    app.add_routes(
        [
            web.get("/", page),
            web.get("/health", health),
            web.get("/{name:app\\.(?:js|css)}", asset),
            web.post("/api/state", api_state),
            web.post("/api/login/start", api_login_start),
            web.post("/api/login/code", api_login_code),
            web.post("/api/login/password", api_login_password),
            web.post("/api/login/cancel", api_login_cancel),
            web.post("/api/account/verify", api_account_verify),
            web.post("/api/account/forget", api_account_forget),
        ]
    )
    return app


async def serve() -> web.AppRunner:
    """Поднять сервер. Возвращает runner — его закрывают при остановке."""
    runner = web.AppRunner(build(), access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, config.HOST, config.PORT)
    await site.start()
    log.info(
        "мини-апп слушает %s:%s%s",
        config.HOST,
        config.PORT,
        f", адрес для кнопки: {config.WEBAPP_URL}" if config.WEBAPP_URL else "",
    )
    return runner
