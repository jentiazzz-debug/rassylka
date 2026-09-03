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
import broadcast
import chats
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


async def _campaign_view(campaign: db.Campaign) -> dict:
    targets = await broadcast._targets(campaign)
    return {
        "id": campaign.id,
        "account_id": campaign.account_id,
        "title": campaign.title,
        "text": campaign.text,
        "interval": campaign.interval,
        "source": campaign.source,
        "folder_id": campaign.folder_id,
        "status": campaign.status,
        "chats": len(targets),
        "next_run_at": campaign.next_run_at,
        "sent_ok": campaign.sent_ok,
        "sent_err": campaign.sent_err,
        "cycles": campaign.cycles,
        "note": campaign.note,
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
        "campaigns": [
            await _campaign_view(c) for c in await db.campaigns(user_id)
        ],
        "limits": {
            "max_accounts": config.MAX_ACCOUNTS,
            "trial_days": config.TRIAL_DAYS,
            "min_interval": config.MIN_INTERVAL,
            "daily_limit": config.DAILY_LIMIT,
            "max_text": config.MAX_TEXT,
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


# --- чаты и папки -----------------------------------------------------


@authed
async def api_chats(request: web.Request, user: dict, data: dict) -> web.Response:
    """Кэш чатов и папок аккаунта — то, из чего выбирают при создании."""
    user_id = int(user["id"])
    account_id = _int(data.get("account_id"))
    account = await db.account(user_id, account_id)
    if account is None:
        return _fail("Такого аккаунта нет.")
    found = await db.chats(account.id)
    return web.json_response(
        {
            "ok": True,
            "chats": [
                {
                    "id": chat.chat_id,
                    "title": chat.title,
                    "username": chat.username,
                    # Тип для показа, а не внутренний: у Telegram канал и
                    # супергруппа — один тип «channel», и называть в
                    # списке группу каналом нельзя.
                    "kind": (
                        "channel"
                        if chat.broadcast
                        else "user" if chat.kind == "user" else "group"
                    ),
                }
                for chat in found
            ],
            "folders": [
                {
                    "id": folder.folder_id,
                    "title": folder.title,
                    "chats": len(folder.chat_ids),
                }
                for folder in await db.folders(account.id)
            ],
        }
    )


@authed
async def api_chats_scan(request: web.Request, user: dict, data: dict) -> web.Response:
    """Перечитать диалоги у Telegram. Тяжёлый запрос — только по кнопке."""
    try:
        found = await chats.scan(int(user["id"]), _int(data.get("account_id")))
    except accounts.LoginError as error:
        return _fail(error.message)
    return web.json_response({"ok": True, **found})


# --- рассылки ---------------------------------------------------------


@authed
async def api_campaign_create(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])

    subscription = await db.subscription(user_id)
    if not subscription.active:
        return _fail(
            "Бесплатный период закончился — новые рассылки не создаются. "
            "Напишите в поддержку."
        )

    account = await db.account(user_id, _int(data.get("account_id")))
    if account is None:
        return _fail("Выберите аккаунт.")
    if account.status != "ok":
        return _fail("Этот аккаунт не в сети — подключите его заново.")

    text = str(data.get("text") or "").strip()
    if not text:
        return _fail("Напишите текст сообщения.")
    if len(text) > config.MAX_TEXT:
        return _fail(f"Слишком длинный текст: максимум {config.MAX_TEXT} символов.")

    interval = _int(data.get("interval"))
    if interval < config.MIN_INTERVAL:
        # Планка не техническая, а защитная: чаще — это заявка на
        # блокировку аккаунта, и человеку лучше узнать об этом здесь.
        return _fail(
            f"Интервал меньше {config.MIN_INTERVAL // 60 or 1} мин — так "
            "Telegram ограничит аккаунт. Поставьте больше."
        )

    source = "folder" if data.get("source") == "folder" else "chats"
    folder_id = None
    chat_ids: list[int] = []

    if source == "folder":
        folder_id = _int(data.get("folder_id"))
        folder = await db.folder(account.id, folder_id)
        if folder is None:
            return _fail("Папка не найдена — обновите список чатов.")
        if not folder.chat_ids:
            return _fail("В этой папке нет чатов.")
    else:
        wanted = data.get("chat_ids")
        wanted = [_int(v) for v in wanted] if isinstance(wanted, list) else []
        # Сверяем с кэшем аккаунта: список приходит из браузера, и
        # принимать оттуда произвольные id нельзя — так можно было бы
        # заказать рассылку в чат, которого у аккаунта нет.
        known = {chat.chat_id for chat in await db.chats(account.id)}
        chat_ids = [chat_id for chat_id in wanted if chat_id in known]
        if not chat_ids:
            return _fail("Выберите хотя бы один чат.")

    title = str(data.get("title") or "").strip()[:60] or text.split("\n")[0][:40]
    campaign_id = await db.create_campaign(
        user_id,
        account.id,
        title=title,
        text=text,
        interval=interval,
        source=source,
        folder_id=folder_id,
        chat_ids=chat_ids,
        # Первое сообщение уходит сразу: человек только что нажал
        # «Запустить» и ждёт увидеть результат, а не через час.
        start_at=int(time.time()),
    )
    campaign = await db.campaign(user_id, campaign_id)
    return web.json_response(
        {"ok": True, "campaign": await _campaign_view(campaign)}
    )


@authed
async def api_campaign_toggle(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    campaign = await db.campaign(user_id, _int(data.get("id")))
    if campaign is None:
        return _fail("Рассылка не найдена.")

    if campaign.status == "running":
        await db.set_campaign_status(campaign.id, "paused", None)
    else:
        subscription = await db.subscription(user_id)
        if not subscription.active:
            return _fail("Бесплатный период закончился. Напишите в поддержку.")
        account = await db.account(user_id, campaign.account_id)
        if account is None or account.status != "ok":
            return _fail("Аккаунт этой рассылки не в сети — подключите его заново.")
        await db.set_campaign_status(campaign.id, "running", None)
        await db.reschedule_campaign(campaign.id, int(time.time()))

    return web.json_response(
        {"ok": True, "campaign": await _campaign_view(await db.campaign(user_id, campaign.id))}
    )


@authed
async def api_campaign_delete(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    if not await db.delete_campaign(int(user["id"]), _int(data.get("id"))):
        return _fail("Рассылка не найдена.")
    return web.json_response({"ok": True})


@authed
async def api_campaign_log(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    campaign = await db.campaign(int(user["id"]), _int(data.get("id")))
    if campaign is None:
        return _fail("Рассылка не найдена.")
    return web.json_response({"ok": True, "sends": await db.sends(campaign.id)})


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
            web.post("/api/chats", api_chats),
            web.post("/api/chats/scan", api_chats_scan),
            web.post("/api/campaign/create", api_campaign_create),
            web.post("/api/campaign/toggle", api_campaign_toggle),
            web.post("/api/campaign/delete", api_campaign_delete),
            web.post("/api/campaign/log", api_campaign_log),
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
