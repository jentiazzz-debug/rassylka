"""Оплата криптой через @xRocket (xRocket Pay API).

Второй криптокошелёк рядом с @CryptoBot, а не вместо него: у людей
кошелёк уже какой-то один, и предлагать «заведите нужный» — верный
способ не получить оплату вовсе. Цена в обоих одна и та же: пачки берутся
из тех же `CRYPTO_PACKS`, курс — из того же `COINS_PER_USD`. Разные цены
за одно и то же в двух кошельках выглядели бы наценкой за выбор.

Счёт выставляется в USDT — он привязан к доллару, и цена в приложении
совпадает с тем, что человек увидит в кошельке.

Про подтверждение оплаты. У xRocket есть вебхук, но здесь, как и у
CryptoBot, выбран опрос: вебхук требует публичного адреса, отдельной
настройки в кабинете и проверки подписи, а опрос по своим счетам даёт
тот же результат и не ломается при переезде домена. Счета живут в той же
таблице `invoices`, что и рублёвые, и добиваются той же воркер-сверкой.

Опрашиваем **каждый счёт по отдельности** (`GET /tg-invoices/{id}`), а
не общим списком: список у xRocket листается страницами и отдаёт все
счета приложения подряд, включая чужие по времени. Висящих счетов
единицы, и точечный запрос честнее, чем разбор страниц.

Начисление, как и везде, идёт **по нашей записи о счёте**, а не по
числам из ответа: сумма в ответе приходит снаружи.
"""

from __future__ import annotations

import json
import logging
import uuid

import aiohttp

import config
import db
import texts

log = logging.getLogger("rassylka.xrocket")

TIMEOUT = aiohttp.ClientTimeout(total=20)

#: Сколько живёт счёт. Сутки — потолок самого xRocket; берём час: за час
#: оплачивают или не оплачивают вовсе, а протухший счёт лучше живого —
#: по нему не заплатят вчерашнюю цену.
INVOICE_TTL = 3600


def ready() -> bool:
    return bool(config.XROCKET_TOKEN)


def price_of(coins: int) -> float | None:
    """Цена пачки в долларах. None — такой пачки нет."""
    for pack in config.CRYPTO_PACKS:
        if pack["coins"] == coins:
            return pack["usd"]
    return None


def min_coins() -> int:
    """Меньше этого криптой не продать: счёт на центы съест комиссия."""
    from math import ceil

    return max(config.COIN_MIN,
               ceil(config.CRYPTO_MIN_USD * config.COINS_PER_USD))


def price_for(coins: int) -> float | None:
    """Цена любого количества монет. None — столько продать нельзя.

    Готовая пачка идёт по своей цене: в ней заложена скидка, и считать
    её по базовому курсу значило бы отменить скидку тому, кто ввёл то же
    число руками.
    """
    exact = price_of(coins)
    if exact is not None:
        return exact
    if not (min_coins() <= coins <= config.COIN_MAX):
        return None
    return round(coins / config.COINS_PER_USD, 2)


async def _call(method: str, path: str, payload: dict | None = None) -> dict:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.request(
            method,
            f"{config.XROCKET_API}{path}",
            headers={"Rocket-Pay-Key": config.XROCKET_TOKEN},
            json=payload,
        ) as answer:
            body = await answer.json()
    if not body.get("success"):
        # У xRocket ошибки приходят и строкой, и списком по полям.
        problem = body.get("message") or body.get("errors") or body
        raise RuntimeError(f"xRocket: {problem}")
    return body.get("data") or {}


async def create(user_id: int, coins: int) -> dict:
    """Выставить счёт в USDT. Возвращает {'url': ..., 'id': ...}."""
    if not ready():
        raise RuntimeError("xRocket не настроен")
    usd = price_for(coins)
    if usd is None:
        raise ValueError("столько монет так не продать")

    order_id = uuid.uuid4().hex
    data = await _call("POST", "/tg-invoices", {
        "amount": round(usd, 2),
        # Счёт одноразовый: иначе по одной ссылке заплатят дважды, а
        # начислим мы один раз — свою запись о счёте мы закрываем сразу.
        "numPayments": 1,
        "currency": config.XROCKET_ASSET,
        "description": texts.pack_title(coins),
        "hiddenMessage": "Монеты уже на счету — откройте приложение.",
        "commentsEnabled": False,
        "payload": json.dumps({"user_id": user_id, "coins": coins,
                               "order": order_id}),
        "expiredIn": INVOICE_TTL,
    })

    invoice_id = str(data.get("id") or "")
    url = data.get("link")
    if not invoice_id or not url:
        raise RuntimeError("xRocket не вернул ссылку на оплату")

    await db.open_invoice(
        f"xrocket:{invoice_id}", order_id, user_id, coins, usd,
        provider="xrocket",
    )
    log.info("счёт xRocket: %s монет за $%s, человек %s", coins, usd, user_id)
    return {"url": url, "id": invoice_id}


async def reconcile(bot) -> int:
    """Проверить свои висящие счета и начислить оплаченные."""
    if not ready():
        return 0
    pending = [
        row for row in await db.pending_invoices(max_age=86400)
        if str(row.get("provider") or "") == "xrocket"
    ]

    done = 0
    for row in pending:
        key = row["transaction_id"]
        try:
            data = await _call("GET", f"/tg-invoices/{key.split(':', 1)[1]}")
        except Exception as error:
            # Один недоступный счёт не повод бросать остальные: следующая
            # сверка через пять минут переспросит.
            log.debug("опрос счёта xRocket %s: %s", key, error)
            continue

        status = str(data.get("status") or "")
        if status == "paid":
            if await _credit(key, bot):
                done += 1
        elif status == "expired":
            await db.close_invoice(key, "expired")
    return done


async def _credit(transaction_id: str, bot) -> bool:
    """Начислить монеты по оплаченному счёту. False — уже начисляли."""
    invoice = await db.invoice(transaction_id)
    if invoice is None or invoice["status"] != "pending":
        return False
    # close_invoice меняет статус только у «pending» и одним запросом:
    # две одновременные сверки иначе начислили бы дважды.
    if not await db.close_invoice(transaction_id, "paid"):
        return False

    coins = int(invoice["coins"])
    user_id = int(invoice["user_id"])
    await db.record_payment(
        transaction_id, user_id, 0, coins, f"usd:{invoice['rub']}"
    )
    balance = await db.add_coins(
        user_id, coins, f"покупка за ${invoice['rub']:.2f}"
    )
    log.info("оплата через xRocket: $%s → %s монет, человек %s",
             invoice["rub"], coins, user_id)

    if bot is not None:
        try:
            await bot.send_message(
                user_id,
                texts.crypto_payment_done(coins, invoice["rub"], balance),
            )
        except Exception as error:
            log.debug("уведомление об оплате криптой не ушло: %s", error)

    import payments

    await payments.reward_referrer(bot, user_id, coins)
    return True
