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
import os
import tempfile
import time
import urllib.parse
from functools import wraps
from pathlib import Path

from aiohttp import web

import accounts
import broadcast
import chats
import comments
import config
import cryptobot
import xrocket
import db
import faq
import handlers
import legal
import payments
import platega
import tdata
import texts
import tickets

log = logging.getLogger("rassylka.webapp")

#: Бот. Нужен только для счетов на оплату: ссылку на счёт выписывает
#: он, а не веб-сервер. Ставится из main.py при запуске.
_bot = None


def use_bot(bot) -> None:
    global _bot
    _bot = bot


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
        "source": account.source,
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
        "content": campaign.content,
        "saved_id": campaign.saved_id,
        "pick": campaign.pick,
        "variants": await db.variants(campaign),
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
        "watches": await _watch_cards(user_id),
        "paid_max_stars": await db.paid_max_stars(user_id),
        "tickets": [_ticket_card(row) for row in await db.tickets_of(user_id)],
        "faq": faq.items(),
        # Приложение не прячем: человек должен видеть, что доступ
        # закрыт, и почему. Пустой экран без объяснения он примет за
        # поломку и придёт спрашивать — уже другим способом.
        "banned": (await db.is_banned(user_id))[0],
        "ban_reason": (await db.is_banned(user_id))[1],
        "limits": {
            "max_accounts": config.MAX_ACCOUNTS,
            "trial_days": config.TRIAL_DAYS,
            "min_interval": config.MIN_INTERVAL,
            "daily_limit": config.DAILY_LIMIT,
            "max_text": config.MAX_TEXT,
            "max_watches": config.MAX_WATCHES,
            "max_upload_mb": config.MAX_UPLOAD_MB,
            "tickets_per_day": config.TICKETS_PER_DAY,
            "paid_cap": config.PAID_MAX_STARS,
            "comment_min_delay": config.COMMENT_MIN_DELAY,
            "max_variants": config.MAX_VARIANTS,
        },
        "banner": {
            "title": await db.setting("app_banner_title"),
            "text": await db.setting("app_banner_text"),
        },
        "is_admin": user_id in config.ADMIN_IDS,
        "mtproto_ready": config.mtproto_ready(),
        "tdata_ready": tdata.available(),
        "max_tdata_mb": config.MAX_TDATA_MB,
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


# --- профиль и оплата -------------------------------------------------


@authed
async def api_profile(request: web.Request, user: dict, data: dict) -> web.Response:
    """Профиль: кто это, монеты, приглашения, тарифы, пачки монет."""
    user_id = int(user["id"])
    invite = ""
    if _bot is not None:
        try:
            invite = await handlers.invite_link(_bot, user_id)
        except Exception as error:
            log.debug("ссылку приглашения не собрать: %s", error)
    return web.json_response(
        {
            "ok": True,
            # Аватарку и имя берём из подписанной initData, а не из базы:
            # Telegram отдаёт их вместе с подписью, и они всегда свежие.
            "photo_url": user.get("photo_url"),
            "name": " ".join(
                p for p in (user.get("first_name"), user.get("last_name")) if p
            ) or "Без имени",
            "username": user.get("username"),
            "id": user_id,
            "is_premium": bool(user.get("is_premium")),
            "coin_name": config.COIN_NAME,
            "stats": await db.profile(user_id),
            "plans": [
                {"days": plan["days"], "coins": plan["stars"]}
                for plan in config.PLANS
            ],
            "packs": [
                {
                    "coins": pack["coins"],
                    "stars": pack["stars"],
                    "base": payments.base_price(pack["coins"]),
                    "popular": pack["coins"] == config.COIN_PACK_POPULAR,
                }
                for pack in config.COIN_PACKS
            ],
            "crypto_packs": [
                {"coins": pack["coins"], "usd": pack["usd"]}
                for pack in config.CRYPTO_PACKS
            ] if cryptobot.ready() else [],
            "xrocket_packs": [
                {"coins": pack["coins"], "usd": pack["usd"]}
                for pack in config.CRYPTO_PACKS
            ] if xrocket.ready() else [],
            "coins_per_usd": config.COINS_PER_USD,
            "rub_per_coin": config.RUB_PER_COIN,
            # Нижний порог у каждого способа свой: у эквайринга своя
            # минимальная сумма, у криптокошелька — своя. Одно общее
            # число здесь врало бы про половину способов.
            "min_coins": {
                "stars": config.COIN_MIN,
                "rub": platega.min_coins(),
                "crypto": cryptobot.min_coins(),
                "xrocket": xrocket.min_coins(),
            },
            "rub_packs": [
                {"coins": pack["coins"], "rub": pack["rub"]}
                for pack in config.RUB_PACKS
            ] if config.platega_ready() else [],
            "legal": {
                "terms": f"{config.WEBAPP_URL}/terms" if config.WEBAPP_URL else "/terms",
                "privacy": f"{config.WEBAPP_URL}/privacy" if config.WEBAPP_URL else "/privacy",
                "tariffs": f"{config.WEBAPP_URL}/tariffs" if config.WEBAPP_URL else "/tariffs",
                "support": f"{config.WEBAPP_URL}/support" if config.WEBAPP_URL else "/support",
            },
            "custom": {
                "min": config.COIN_MIN,
                "max": config.COIN_MAX,
                "rate": config.STARS_PER_COIN,
            },
            "history": await db.coin_history(user_id, 10),
            "referral": {
                **await db.referral_stats(user_id),
                "link": invite,
                "coins": config.REF_COINS,
                "percent": config.REF_PERCENT,
            },
        }
    )


def _too_small(minimum: int) -> str:
    """Отказ с числом, а не с «нельзя»: иначе непонятно, что вводить."""
    return (
        f"Этим способом можно купить от {minimum} до {config.COIN_MAX} "
        f"{config.COIN_NAME}."
    )


@authed
async def api_invoice(request: web.Request, user: dict, data: dict) -> web.Response:
    """Счёт на пачку монет: его мини-апп открывает через tg.openInvoice.

    Монеты здесь никто не начисляет. Ответ openInvoice приходит из
    браузера, и верить ему нельзя — настоящее подтверждение приезжает
    боту отдельным апдейтом от Telegram (см. payments.py).
    """
    if _bot is None:
        return _fail("Оплата сейчас недоступна. Напишите в поддержку.")
    coins = _int(data.get("coins"))
    if not payments.sellable(coins):
        return _fail(
            f"Можно купить от {config.COIN_MIN} до {config.COIN_MAX} "
            f"{config.COIN_NAME}."
        )
    try:
        link = await payments.invoice_link(_bot, coins)
    except Exception as error:
        log.exception("счёт на %s монет не создался", coins)
        return _fail(f"Счёт не создался: {type(error).__name__}")
    return web.json_response({"ok": True, "link": link})


@authed
async def api_subscribe(request: web.Request, user: dict, data: dict) -> web.Response:
    """Купить подписку за монеты."""
    user_id = int(user["id"])
    days = _int(data.get("days"))
    ok, error = await payments.buy_subscription(user_id, days)
    if not ok:
        return _fail(error)
    subscription = await db.subscription(user_id)
    balance = await db.coins_of(user_id)
    # Сообщение в личку, а не только в приложении: человек его закроет, а
    # подтверждение покупки должно остаться где-то, где его найдут.
    if _bot is not None:
        try:
            await _bot.send_message(
                user_id,
                texts.subscribed(
                    days, payments.plan_price(days), subscription.until, balance
                ),
            )
        except Exception as error:
            log.debug("подтверждение подписки не ушло: %s", error)
    return web.json_response(
        {"ok": True, "coins": balance, "until": subscription.until}
    )


# --- материалы --------------------------------------------------------


@authed
async def api_materials(request: web.Request, user: dict, data: dict) -> web.Response:
    """Сообщения из «Избранного» аккаунта — то, что можно рассылать."""
    account = await db.account(int(user["id"]), _int(data.get("account_id")))
    if account is None:
        return _fail("Такого аккаунта нет.")
    return web.json_response(
        {"ok": True, "materials": await db.materials(account.id)}
    )


# --- импорт из tdata --------------------------------------------------


async def api_account_tdata(request: web.Request) -> web.Response:
    """Приём архива с tdata.

    Отдельно от остальных ручек и без декоратора: тело здесь не JSON, а
    multipart с файлом, и подпись приходится брать из заголовка. Файл
    пишется на диск потоком с проверкой размера — целиком в память его
    принимать нельзя, туда прилетит что угодно.
    """
    user = parse_init_data(request.headers.get(INIT_HEADER, ""))
    if user is None:
        return web.json_response(
            {"ok": False, "error": "Откройте приложение из бота заново."},
            status=401,
        )
    user_id = int(user["id"])

    if not tdata.available():
        return _fail(
            "Импорт из tdata не настроен на сервере. Напишите в поддержку."
        )
    if await db.count_accounts(user_id) >= config.MAX_ACCOUNTS:
        return _fail(
            f"Больше {config.MAX_ACCOUNTS} аккаунтов подключить нельзя."
        )

    limit = config.MAX_TDATA_MB * 1024 * 1024
    passcode = ""
    archive: Path | None = None

    try:
        reader = await request.multipart()
    except Exception:
        return _fail("Файл не пришёл. Попробуйте ещё раз.")

    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "passcode":
                # Код-пароль нигде не сохраняется и не пишется в лог: он
                # нужен ровно на время разбора архива.
                passcode = (await part.text()).strip()
                continue
            if part.name != "file":
                continue

            handle, path = tempfile.mkstemp(suffix=".zip", dir=config.DATA_DIR)
            archive = Path(path)
            size = 0
            with os.fdopen(handle, "wb") as out:
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        return _fail(
                            f"Архив больше {config.MAX_TDATA_MB} МБ. "
                            "Заархивируйте только папку tdata."
                        )
                    out.write(chunk)

        if archive is None or not archive.exists() or archive.stat().st_size == 0:
            return _fail("Прикрепите zip-архив с папкой tdata.")

        try:
            result = await tdata.import_zip(user_id, archive, passcode)
        except tdata.TdataError as error:
            return _fail(error.message)

        if not result["added"]:
            reason = (result["failed"] or [{}])[0].get("error")
            return _fail(
                "Ни один аккаунт подключить не вышло"
                + (f": {reason}." if reason else ".")
            )
        return web.json_response(
            {
                "ok": True,
                "added": result["added"],
                "failed": result["failed"],
                "accounts": [
                    _account_view(a) for a in await db.accounts(user_id)
                ],
            }
        )
    finally:
        # Архив с чужими ключами не должен остаться на диске ни при
        # удаче, ни при ошибке, ни при обрыве загрузки.
        if archive is not None:
            archive.unlink(missing_ok=True)


async def api_material_upload(request: web.Request) -> web.Response:
    """Приём файла и отправка его в «Избранное» аккаунта.

    Без декоратора и отдельно от JSON-ручек: тело здесь multipart, и
    подпись приходится брать из заголовка. Файл пишется на диск потоком
    с проверкой размера — целиком в память его принимать нельзя, туда
    прилетит что угодно.

    У нас файл не остаётся: он уходит в «Избранное» аккаунта и стирается
    с диска в любом случае — и при удаче, и при ошибке, и при обрыве.
    Своего хранилища чужих фото и голосовых мы не заводим.
    """
    user = parse_init_data(request.headers.get(INIT_HEADER, ""))
    if user is None:
        return web.json_response(
            {"ok": False, "error": "Откройте приложение из бота заново."},
            status=401,
        )
    user_id = int(user["id"])

    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    account_id = 0
    as_file = False
    filename = "file"
    saved: Path | None = None

    try:
        reader = await request.multipart()
    except Exception:
        return _fail("Файл не пришёл. Попробуйте ещё раз.")

    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "account_id":
                account_id = _int(await part.text())
                continue
            if part.name == "as_file":
                as_file = (await part.text()).strip() in {"1", "true", "yes"}
                continue
            if part.name != "file":
                continue

            filename = (part.filename or "file")[:96]
            suffix = Path(filename).suffix[:12]
            handle, path = tempfile.mkstemp(suffix=suffix, dir=config.DATA_DIR)
            saved = Path(path)
            size = 0
            with os.fdopen(handle, "wb") as out:
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        return _fail(
                            f"Файл больше {config.MAX_UPLOAD_MB} МБ."
                        )
                    out.write(chunk)

        if saved is None or not saved.exists() or saved.stat().st_size == 0:
            return _fail("Прикрепите файл.")

        account = await db.account(user_id, account_id)
        if account is None:
            return _fail("Выберите аккаунт.")
        if account.status != "ok":
            return _fail("Этот аккаунт не в сети — подключите его заново.")

        try:
            result = await chats.upload_to_saved(
                user_id, account_id, saved, filename, as_file
            )
        except accounts.LoginError as error:
            return _fail(error.message)
        return web.json_response({"ok": True, **result})
    finally:
        if saved is not None:
            saved.unlink(missing_ok=True)


# --- поддержка --------------------------------------------------------


def _ticket_card(row: dict) -> dict:
    return {
        "id": row["id"],
        "subject": row["subject"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _ticket_message(row: dict) -> dict:
    return {
        "author": row["author"],
        "text": row["text"],
        "file_kind": row["file_kind"],
        "file_name": row["file_name"],
        "created_at": row["created_at"],
    }


@authed
async def api_tickets(request: web.Request, user: dict, data: dict) -> web.Response:
    user_id = int(user["id"])
    found = await db.tickets_of(user_id)
    return web.json_response(
        {"ok": True, "tickets": [_ticket_card(row) for row in found]}
    )


@authed
async def api_ticket_read(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    ticket_id = _int(data.get("ticket_id"))
    # user_id в запросе к базе, а не проверкой после: чужой тикет не
    # должен даже читаться, а забыть про сравнение проще, чем про аргумент.
    found = await db.ticket(ticket_id, user_id)
    if found is None:
        return _fail("Обращение не найдено.")
    return web.json_response({
        "ok": True,
        "ticket": _ticket_card(found),
        "messages": [
            _ticket_message(row) for row in await db.ticket_messages(ticket_id)
        ],
    })


@authed
async def api_ticket_close(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    found = await db.ticket(_int(data.get("ticket_id")), user_id)
    if found is None:
        return _fail("Обращение не найдено.")
    await db.set_ticket_status(found["id"], "closed")
    return web.json_response({
        "ok": True,
        "tickets": [_ticket_card(row) for row in await db.tickets_of(user_id)],
    })


async def api_ticket_send(request: web.Request) -> web.Response:
    """Новый тикет или ответ в существующий — вместе с вложением.

    Одна ручка на оба случая и без декоратора: тело здесь multipart, а
    не JSON. Разделять их на две значило бы дважды написать приём файла
    ради одного различающегося поля.
    """
    user = parse_init_data(request.headers.get(INIT_HEADER, ""))
    if user is None:
        return web.json_response(
            {"ok": False, "error": "Откройте приложение из бота заново."},
            status=401,
        )
    user_id = int(user["id"])
    await db.ensure_user(user_id, user.get("username"), None)

    if _bot is None:
        return _fail("Поддержка сейчас недоступна. Напишите позже.")

    banned, reason = await db.is_banned(user_id)
    if banned:
        # Причину показываем: молчаливый отказ выглядит поломкой, и
        # человек начнёт долбиться в него снова и снова.
        return _fail(
            "Доступ к поддержке закрыт"
            + (f": {reason}." if reason else ".")
        )

    limit = config.MAX_UPLOAD_MB * 1024 * 1024
    subject = body = ""
    ticket_id = 0
    saved: Path | None = None
    filename = ""

    try:
        reader = await request.multipart()
    except Exception:
        return _fail("Сообщение не дошло. Попробуйте ещё раз.")

    try:
        while True:
            part = await reader.next()
            if part is None:
                break
            if part.name == "subject":
                subject = (await part.text()).strip()[:tickets.MAX_SUBJECT]
                continue
            if part.name == "body":
                body = (await part.text()).strip()[:tickets.MAX_BODY]
                continue
            if part.name == "ticket_id":
                ticket_id = _int(await part.text())
                continue
            if part.name != "file":
                continue

            filename = (part.filename or "file")[:96]
            handle, path = tempfile.mkstemp(
                suffix=Path(filename).suffix[:12], dir=config.DATA_DIR
            )
            saved = Path(path)
            size = 0
            with os.fdopen(handle, "wb") as out:
                while True:
                    chunk = await part.read_chunk(256 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        return _fail(f"Файл больше {config.MAX_UPLOAD_MB} МБ.")
                    out.write(chunk)

        if not body and saved is None:
            return _fail("Опишите, что случилось.")

        file_kind = file_id = None
        if saved is not None:
            # Файл уходит владельцу и превращается в file_id: по нему
            # Telegram потом отдаст его сколько угодно раз, а у нас на
            # диске не останется ничего.
            file_kind, file_id = await _stash_file(saved, filename)
            if file_id is None:
                return _fail("Файл не отправился. Попробуйте другой.")

        card = await db.user_card(user_id) or {}
        person = card.get("user") or {"user_id": user_id}

        if ticket_id:
            found = await db.ticket(ticket_id, user_id)
            if found is None:
                return _fail("Обращение не найдено.")
            if found["status"] == "closed":
                return _fail("Обращение закрыто. Заведите новое.")
            await tickets.user_reply(
                _bot, person, ticket_id, body, file_kind, file_id, filename or None
            )
        else:
            if not subject:
                return _fail("Напишите тему обращения.")
            if await db.count_tickets_today(user_id) >= config.TICKETS_PER_DAY:
                return _fail(
                    f"Больше {config.TICKETS_PER_DAY} обращений в сутки "
                    "завести нельзя. Допишите в уже открытое."
                )
            ticket_id = await tickets.open_ticket(
                _bot, person, subject, body, file_kind, file_id,
                filename or None,
            )

        return web.json_response({
            "ok": True,
            "ticket_id": ticket_id,
            "tickets": [_ticket_card(row) for row in await db.tickets_of(user_id)],
        })
    finally:
        if saved is not None:
            saved.unlink(missing_ok=True)


async def _stash_file(path: Path, filename: str) -> tuple[str | None, str | None]:
    """Отправить файл владельцу и вернуть (тип, file_id).

    Отправляем первому владельцу из списка: файл нужен и ему на глаза, и
    нам ради file_id — двух дел одной отправкой. Если владельцев
    несколько, остальные увидят его вместе с текстом обращения.
    """
    from aiogram.types import FSInputFile

    target = next(iter(config.ADMIN_IDS), 0)
    if not target:
        return None, None

    upload = FSInputFile(str(path), filename=filename)
    lower = filename.lower()
    if lower.endswith((".jpg", ".jpeg", ".png", ".webp")):
        sent = await _bot.send_photo(target, upload)
    elif lower.endswith((".mp4", ".mov")):
        sent = await _bot.send_video(target, upload)
    elif lower.endswith(".gif"):
        sent = await _bot.send_animation(target, upload)
    elif lower.endswith((".ogg", ".oga")):
        sent = await _bot.send_voice(target, upload)
    else:
        sent = await _bot.send_document(target, upload)

    return tickets.file_from(sent)[:2]


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
                    # Отдельным полем, а не выводом из kind: под постами
                    # комментируют только каналы, и приложению нужно
                    # отличать их без разбора строк.
                    "broadcast": bool(chat.broadcast),
                }
                for chat in found
            ],
            "folders": [
                {
                    "id": folder.folder_id,
                    "title": folder.title,
                    "chats": len(folder.chat_ids),
                    # Состав папки отдаём целиком: приложение по нажатию
                    # отмечает все её чаты, а не хранит ссылку на папку.
                    "chat_ids": folder.chat_ids,
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


#: Чему Telegram не даёт подписи. К стикеру и кружку текст не пристаёт
#: — не по нашему решению, а по устройству формата.
NO_CAPTION = {"sticker", "round"}


def _variants(data: dict, known_materials: dict[int, str]) -> tuple[list[dict], str]:
    """Разобрать варианты сообщения из запроса.

    Вариант — это текст, медиа из «Избранного» или то и другое вместе. В
    последнем случае текст уходит подписью к медиа: так фото и слова
    приходят в чат одним сообщением, а не двумя.

    Материалы сверяются с кэшем аккаунта: список приходит из браузера, и
    принимать оттуда произвольные номера сообщений нельзя.
    """
    raw = data.get("variants")
    if not isinstance(raw, list):
        return [], "Добавьте хотя бы одно сообщение."

    out: list[dict] = []
    for item in raw[: config.MAX_VARIANTS]:
        if not isinstance(item, dict):
            continue

        text = str(item.get("text") or "").strip()
        if len(text) > config.MAX_TEXT:
            return [], (
                f"Слишком длинный текст: максимум {config.MAX_TEXT} символов."
            )

        if item.get("content") == "saved":
            saved_id = _int(item.get("saved_id"))
            if saved_id not in known_materials:
                continue
            if text and known_materials[saved_id] in NO_CAPTION:
                return [], (
                    "К стикеру и кружку подпись не пристаёт — Telegram её не "
                    "примет. Уберите текст у этого варианта или прикрепите "
                    "что-то другое."
                )
            out.append({"content": "saved", "text": text, "saved_id": saved_id})
            continue

        if not text:
            continue
        out.append({"content": "text", "text": text, "saved_id": None})

    if not out:
        return [], "Добавьте хотя бы одно сообщение."
    return out, ""


def _watch_card(watch: db.Watch, variants: list[dict]) -> dict:
    return {
        "id": watch.id,
        "account_id": watch.account_id,
        "chat_id": watch.chat_id,
        "title": watch.title,
        "delay_min": watch.delay_min,
        "delay_max": watch.delay_max,
        "pick": watch.pick,
        "variants": variants,
        "status": watch.status,
        "sent_ok": watch.sent_ok,
        "sent_err": watch.sent_err,
        "note": watch.note,
    }


async def _watch_cards(user_id: int) -> list[dict]:
    return [
        _watch_card(watch, await db.watch_variants(watch.id))
        for watch in await db.watches(user_id)
    ]


async def _watch_form(user: dict, data: dict) -> tuple[dict, str]:
    """Разобрать общее для создания и правки: задержка, порядок, тексты."""
    account = await db.account(int(user["id"]), _int(data.get("account_id")))
    if account is None:
        return {}, "Выберите аккаунт."
    if account.status != "ok":
        return {}, "Этот аккаунт не в сети — подключите его заново."

    known = {m["msg_id"]: m["kind"] for m in await db.materials(account.id)}
    variants, error = _variants(data, known)
    if error:
        return {}, error

    low = _int(data.get("delay_min"))
    high = _int(data.get("delay_max"))
    if low < config.COMMENT_MIN_DELAY:
        # Планка стоит только если её задал владелец бота. По умолчанию
        # она нулевая: раздачи «первым десяти» выигрываются секундами, и
        # ждать ради правдоподобия там нечего.
        return {}, (
            f"Задержка меньше {config.COMMENT_MIN_DELAY} с здесь не "
            "разрешена."
        )
    if high < low:
        high = low

    pick = "order" if data.get("pick") == "order" else "random"
    return {
        "account": account,
        "variants": variants,
        "delay_min": low,
        "delay_max": high,
        "pick": pick,
    }, ""


@authed
async def api_paid_limit(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Потолок платы за сообщение.

    Звёзды списываются с подключённого аккаунта, а не с баланса в боте,
    поэтому значение и хранится у человека, а не в настройках сервиса:
    это его деньги и его решение.
    """
    user_id = int(user["id"])
    stars = max(0, _int(data.get("stars")))
    if stars > config.PAID_MAX_STARS:
        return _fail(
            f"Больше {config.PAID_MAX_STARS} ⭐️ за сообщение — это уже не "
            "рассылка. Поставьте меньше."
        )
    await db.set_paid_max_stars(user_id, stars)
    return web.json_response({"ok": True, "paid_max_stars": stars})


@authed
async def api_watch_create(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])

    if (await db.is_banned(user_id))[0]:
        return _fail("Доступ закрыт.")

    subscription = await db.subscription(user_id)
    if not subscription.active:
        return _fail(
            "Бесплатный период закончился — новые автокомментарии не "
            "создаются. Напишите в поддержку."
        )
    if await db.count_watches(user_id) >= config.MAX_WATCHES:
        return _fail(f"Больше {config.MAX_WATCHES} наблюдений не завести.")

    form, error = await _watch_form(user, data)
    if error:
        return _fail(error)
    account = form["account"]

    chat_id = _int(data.get("chat_id"))
    found = await db.chats_by_ids(account.id, [chat_id])
    if not found:
        return _fail("Выберите канал из списка.")
    chat = found[0]
    if not chat.broadcast:
        # Комментарии бывают только под постами канала: в обычной группе
        # комментировать нечего.
        return _fail("Это не канал. Комментарии бывают под постами каналов.")

    # Точка отсчёта — сейчас: отвечать задним числом на посты, которые
    # вышли до того, как человек это включил, никто не просил.
    try:
        last_id = await comments.newest_post(account, chat)
    except Exception as error:
        log.exception("не прочитался канал %s", chat_id)
        return _fail(f"Не удалось прочитать канал: {type(error).__name__}")

    watch_id = await db.create_watch(
        user_id, account.id, chat.chat_id, chat.title or "канал",
        last_id, form["delay_min"], form["delay_max"], form["pick"],
    )
    await db.set_watch_variants(watch_id, form["variants"])
    log.info("автокомментарий %s заведён на «%s»", watch_id, chat.title)
    return web.json_response({"ok": True, "watches": await _watch_cards(user_id)})


@authed
async def api_watch_edit(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    watch = await db.watch(user_id, _int(data.get("watch_id")))
    if watch is None:
        return _fail("Наблюдение не найдено.")

    data = {**data, "account_id": watch.account_id}
    form, error = await _watch_form(user, data)
    if error:
        return _fail(error)

    await db.edit_watch(
        user_id, watch.id, watch.title,
        form["delay_min"], form["delay_max"], form["pick"],
    )
    await db.set_watch_variants(watch.id, form["variants"])
    return web.json_response({"ok": True, "watches": await _watch_cards(user_id)})


@authed
async def api_watch_toggle(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    watch = await db.watch(user_id, _int(data.get("watch_id")))
    if watch is None:
        return _fail("Наблюдение не найдено.")
    if watch.status == "running":
        await db.set_watch_status(watch.id, "paused", "остановлено вручную")
    else:
        await db.set_watch_status(watch.id, "running", None)
        await db.reschedule_watch(watch.id, int(time.time()))
    return web.json_response({"ok": True, "watches": await _watch_cards(user_id)})


@authed
async def api_watch_delete(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])
    if not await db.delete_watch(user_id, _int(data.get("watch_id"))):
        return _fail("Наблюдение не найдено.")
    return web.json_response({"ok": True, "watches": await _watch_cards(user_id)})


@authed
async def api_campaign_create(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    user_id = int(user["id"])

    if (await db.is_banned(user_id))[0]:
        return _fail("Доступ закрыт.")

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

    known_materials = {m["msg_id"]: m["kind"] for m in await db.materials(account.id)}
    variants, error = _variants(data, known_materials)
    if error:
        return _fail(error)

    interval = _int(data.get("interval"))
    if interval < config.MIN_INTERVAL:
        # Планка не техническая, а защитная: чаще — это заявка на
        # блокировку аккаунта, и человеку лучше узнать об этом здесь.
        return _fail(
            f"Интервал меньше {config.MIN_INTERVAL // 60 or 1} мин — так "
            "Telegram ограничит аккаунт. Поставьте больше."
        )

    # Папка больше не хранится ссылкой: приложение раскрывает её в список
    # чатов при выборе, и сюда приходят уже конкретные id. Так человек
    # видит, что именно уйдёт, и может снять лишнее.
    source, folder_id = "chats", None
    wanted = data.get("chat_ids")
    wanted = [_int(v) for v in wanted] if isinstance(wanted, list) else []
    # Сверяем с кэшем аккаунта: список приходит из браузера, и принимать
    # оттуда произвольные id нельзя — так можно было бы заказать рассылку
    # в чат, которого у аккаунта нет.
    known = {chat.chat_id for chat in await db.chats(account.id)}
    chat_ids = [chat_id for chat_id in wanted if chat_id in known]
    if not chat_ids:
        return _fail("Выберите хотя бы один чат.")

    pick = "order" if data.get("pick") == "order" else "random"
    # Первый вариант дублируется в саму запись рассылки: по нему рисуется
    # карточка, и на нём же держатся рассылки, заведённые до появления
    # вариантов.
    first = variants[0]
    text, content, saved_id = first["text"], first["content"], first["saved_id"]

    title = (str(data.get("title") or "").strip()[:60]
             or (text or "Рассылка").split("\n")[0][:40])
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
        content=content,
        saved_id=saved_id,
        pick=pick,
    )
    await db.set_variants(campaign_id, variants)
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
async def api_campaign_edit(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Поменять текст, материал, интервал и название уже созданной рассылки."""
    user_id = int(user["id"])
    campaign = await db.campaign(user_id, _int(data.get("id")))
    if campaign is None:
        return _fail("Рассылка не найдена.")

    known_materials = {
        m["msg_id"]: m["kind"] for m in await db.materials(campaign.account_id)
    }
    variants, error = _variants(data, known_materials)
    if error:
        return _fail(error)

    interval = _int(data.get("interval")) or campaign.interval
    if interval < config.MIN_INTERVAL:
        return _fail(
            f"Интервал меньше {config.MIN_INTERVAL // 60 or 1} мин — так "
            "Telegram ограничит аккаунт."
        )

    pick = "order" if data.get("pick") == "order" else "random"
    first = variants[0]
    text, content, saved_id = first["text"], first["content"], first["saved_id"]

    title = str(data.get("title") or "").strip()[:60] or campaign.title
    await db.edit_campaign(
        user_id, campaign.id,
        title=title, text=text, interval=interval,
        content=content, saved_id=saved_id, pick=pick,
    )
    await db.set_variants(campaign.id, variants)
    return web.json_response(
        {"ok": True, "campaign": await _campaign_view(
            await db.campaign(user_id, campaign.id))}
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


async def legal_page(request: web.Request) -> web.Response:
    """Документы: соглашение, политика, тарифы, поддержка.

    Без проверки initData намеренно: их открывает проверяющий из банка
    или платёжной системы, а у него нет Telegram-клиента — страница,
    требующая подписи, для него просто не откроется.
    """
    render = legal.RENDERERS.get(request.path)
    if render is None:
        raise web.HTTPNotFound()
    return web.Response(
        text=render(),
        content_type="text/html",
        charset="utf-8",
        headers={"Cache-Control": "public, max-age=300"},
    )


async def paid_page(request: web.Request) -> web.Response:
    """Куда Platega возвращает человека после оплаты.

    Монеты здесь не начисляются: возврат происходит в браузере, а
    браузеру верить нельзя. Настоящее подтверждение приходит отдельным
    callback от Platega (см. platega.py).
    """
    failed = request.query.get("failed")
    title = "Оплата не прошла" if failed else "Оплата принята"
    note = (
        "Платёж не завершён. Если деньги списались, напишите в поддержку."
        if failed else
        "Монеты появятся на балансе в течение минуты. Можно закрывать эту "
        "страницу и возвращаться в приложение."
    )
    return web.Response(
        text=(
            "<!doctype html><html lang=ru><head><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{title}</title><style>{legal.STYLE}</style></head><body>"
            f"<main><div class=card><h1>{title}</h1><p>{note}</p></div></main>"
            "</body></html>"
        ),
        content_type="text/html",
        charset="utf-8",
    )


async def platega_callback(request: web.Request) -> web.Response:
    """Callback об оплате рублями.

    Отвечаем 200 всегда, когда запрос наш: Platega повторяет доставку до
    трёх раз, если не ответить за минуту, а повторы нам не нужны — все
    решения по счёту уже приняты внутри apply().
    """
    if not platega.secret_ok(request.headers):
        log.warning("callback Platega с чужим секретом: %s", request.remote)
        return web.json_response({"ok": False}, status=403)
    body = await _body(request)
    try:
        result = await platega.apply(body, _bot)
    except Exception:
        log.exception("callback Platega сорвался: %s", body)
        # 500 заставит Platega повторить — это как раз то, что нужно,
        # когда сломались мы, а не запрос.
        return web.json_response({"ok": False}, status=500)
    log.info("callback Platega %s: %s", body.get("id"), result)
    return web.json_response({"ok": True})


@authed
async def api_crypto_invoice(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Счёт в USDT через @CryptoBot."""
    if not cryptobot.ready():
        return _fail("Оплата криптой сейчас недоступна.")
    coins = _int(data.get("coins"))
    if cryptobot.price_for(coins) is None:
        return _fail(_too_small(cryptobot.min_coins()))
    try:
        invoice = await cryptobot.create(int(user["id"]), coins)
    except Exception as error:
        log.exception("счёт CryptoBot на %s монет не создался", coins)
        return _fail(f"Счёт не создался: {type(error).__name__}")
    return web.json_response({"ok": True, "url": invoice["url"]})


@authed
async def api_xrocket_invoice(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Счёт в USDT через @xRocket."""
    if not xrocket.ready():
        return _fail("Оплата через xRocket сейчас недоступна.")
    coins = _int(data.get("coins"))
    if xrocket.price_for(coins) is None:
        return _fail(_too_small(xrocket.min_coins()))
    try:
        invoice = await xrocket.create(int(user["id"]), coins)
    except Exception as error:
        log.exception("счёт xRocket на %s монет не создался", coins)
        return _fail(f"Счёт не создался: {type(error).__name__}")
    return web.json_response({"ok": True, "url": invoice["url"]})


@authed
async def api_rub_invoice(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Счёт на оплату рублями. Возвращает ссылку на платёжную форму."""
    if not config.platega_ready():
        return _fail("Оплата рублями сейчас недоступна. Напишите в поддержку.")
    coins = _int(data.get("coins"))
    if platega.price_for(coins) is None:
        return _fail(_too_small(platega.min_coins()))
    try:
        invoice = await platega.create(
            int(user["id"]), coins, user.get("username")
        )
    except Exception as error:
        log.exception("счёт Platega на %s монет не создался", coins)
        return _fail(f"Счёт не создался: {type(error).__name__}")
    return web.json_response({"ok": True, "url": invoice["url"]})


# --- админка ----------------------------------------------------------


def _is_admin(user: dict) -> bool:
    return int(user.get("id") or 0) in config.ADMIN_IDS


def admin_only(handler):
    """Ручка только для владельца.

    Проверка идёт по id из подписанной initData, а не по чему-то в теле
    запроса: подделать её без токена бота нельзя. Чужим отвечаем 404, а
    не 403 — незачем подтверждать, что такая ручка вообще есть.
    """

    @wraps(handler)
    async def wrapper(request: web.Request, user: dict, data: dict):
        if not _is_admin(user):
            log.warning("чужой в админке: %s %s", user.get("id"), request.path)
            raise web.HTTPNotFound()
        return await handler(request, user, data)

    return wrapper


@authed
@admin_only
async def api_admin_find(request: web.Request, user: dict, data: dict) -> web.Response:
    """Найти человека по id или нику."""
    found = await db.find_users(str(data.get("query") or ""))
    return web.json_response({
        "ok": True,
        "users": [
            {"id": row["user_id"], "username": row["username"],
             "name": row["name"], "coins": row["coins"],
             "seen_at": row["seen_at"]}
            for row in found
        ],
    })


@authed
@admin_only
async def api_admin_user(request: web.Request, user: dict, data: dict) -> web.Response:
    """Карточка человека: подписка, аккаунты, рассылки, платежи, журнал."""
    card = await db.user_card(_int(data.get("id")))
    if card is None:
        return _fail("Такого человека нет.")
    return web.json_response({"ok": True, "card": card})


@authed
@admin_only
async def api_admin_grant(request: web.Request, user: dict, data: dict) -> web.Response:
    """Выдать монеты или дни подписки — для разбора обращений.

    Каждое начисление попадает в журнал монет с пометкой «от поддержки»:
    через месяц никто не вспомнит, почему у человека лишние монеты, а по
    журналу это видно.
    """
    target = _int(data.get("id"))
    if not await db.get_user_exists(target):
        return _fail("Такого человека нет.")

    coins = _int(data.get("coins"))
    days = _int(data.get("days"))
    if not coins and not days:
        return _fail("Нечего выдавать.")
    if abs(coins) > 100000 or abs(days) > 3650:
        return _fail("Слишком много — проверьте число.")

    done = []
    if coins:
        balance = await db.add_coins(target, coins, "начисление от поддержки")
        done.append(f"монет: {coins:+d}, баланс {balance}")
    if days:
        until = await db.grant_paid(target, days)
        done.append(f"подписка продлена на {days} дн.")

    log.info("админ %s: человеку %s — %s", user["id"], target, "; ".join(done))
    if _bot is not None:
        try:
            await _bot.send_message(target, texts.from_support(coins, days))
        except Exception as error:
            log.debug("уведомление о начислении не ушло: %s", error)
    return web.json_response({"ok": True, "done": "; ".join(done)})


@authed
@admin_only
async def api_admin_campaign(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Остановить или продолжить чужую рассылку.

    Нужно, когда на рассылку пожаловались: остановить её должен уметь
    владелец сервиса, а не только сам человек.
    """
    campaign_id = _int(data.get("id"))
    status = "running" if data.get("status") == "running" else "stopped"
    if not await db.set_campaign_status_admin(campaign_id, status):
        return _fail("Такой рассылки нет.")
    log.info("админ %s: рассылка %s → %s", user["id"], campaign_id, status)
    return web.json_response({"ok": True})


@authed
@admin_only
async def api_admin_ban(
    request: web.Request, user: dict, data: dict
) -> web.Response:
    """Заблокировать человека или снять блокировку.

    Бан — это про поведение, а не про деньги: рассылки забаненного
    останавливаются, но всё, что он купил, остаётся при нём. Возврат
    монет — отдельное решение владельца, и делать его молча нельзя.
    """
    target = _int(data.get("user_id"))
    if target in config.ADMIN_IDS:
        return _fail("Владельца забанить нельзя.")
    if not await db.get_user_exists(target):
        return _fail("Такого человека нет.")

    banned = bool(data.get("banned"))
    reason = str(data.get("reason") or "").strip()[:200]
    await db.set_banned(target, banned, reason)

    if banned:
        stopped = await db.stop_user_work(target, "доступ закрыт")
        log.info("забанен %s: %s (остановлено %s)", target, reason, stopped)
        if _bot is not None:
            try:
                await _bot.send_message(target, texts.banned(reason))
            except Exception:
                pass
    else:
        log.info("разбанен %s", target)

    return web.json_response({"ok": True, "card": await db.user_card(target)})


@authed
@admin_only
async def api_admin_stats(request: web.Request, user: dict, data: dict) -> web.Response:
    return web.json_response({"ok": True, "stats": await db.stats()})


async def health(request: web.Request) -> web.Response:
    """Пинг для хостинга: жив ли процесс."""
    return web.json_response({"ok": True, "logins_pending": accounts.pending_count()})


def build() -> web.Application:
    # client_max_size поднят под архив с tdata: по умолчанию aiohttp
    # отклоняет тело больше мегабайта, а архив на порядок больше.
    # Свой потолок и проверка размера потоком — в api_account_tdata.
    app = web.Application(
        client_max_size=config.MAX_TDATA_MB * 1024 * 1024 + 1024 * 1024
    )
    app.add_routes(
        [
            web.get("/", page),
            web.get("/health", health),
            web.get("/terms", legal_page),
            web.get("/privacy", legal_page),
            web.get("/tariffs", legal_page),
            web.get("/support", legal_page),
            web.get("/paid", paid_page),
            web.post(config.PLATEGA_CALLBACK_PATH, platega_callback),
            web.post("/api/invoice/rub", api_rub_invoice),
            web.post("/api/invoice/crypto", api_crypto_invoice),
            web.post("/api/invoice/xrocket", api_xrocket_invoice),
            web.post("/api/admin/find", api_admin_find),
            web.post("/api/admin/user", api_admin_user),
            web.post("/api/admin/grant", api_admin_grant),
            web.post("/api/admin/campaign", api_admin_campaign),
            web.post("/api/admin/stats", api_admin_stats),
            web.post("/api/admin/ban", api_admin_ban),
            web.get("/{name:app\\.(?:js|css)}", asset),
            web.post("/api/state", api_state),
            web.post("/api/login/start", api_login_start),
            web.post("/api/login/code", api_login_code),
            web.post("/api/login/password", api_login_password),
            web.post("/api/login/cancel", api_login_cancel),
            web.post("/api/account/verify", api_account_verify),
            web.post("/api/account/forget", api_account_forget),
            web.post("/api/account/tdata", api_account_tdata),
            web.post("/api/material/upload", api_material_upload),
            web.post("/api/profile", api_profile),
            web.post("/api/invoice", api_invoice),
            web.post("/api/subscribe", api_subscribe),
            web.post("/api/materials", api_materials),
            web.post("/api/chats", api_chats),
            web.post("/api/chats/scan", api_chats_scan),
            web.post("/api/paid-limit", api_paid_limit),
            web.post("/api/tickets", api_tickets),
            web.post("/api/ticket/read", api_ticket_read),
            web.post("/api/ticket/close", api_ticket_close),
            web.post("/api/ticket/send", api_ticket_send),
            web.post("/api/watch/create", api_watch_create),
            web.post("/api/watch/edit", api_watch_edit),
            web.post("/api/watch/toggle", api_watch_toggle),
            web.post("/api/watch/delete", api_watch_delete),
            web.post("/api/campaign/create", api_campaign_create),
            web.post("/api/campaign/edit", api_campaign_edit),
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
