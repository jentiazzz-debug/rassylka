"""Оплата криптой через @CryptoBot (Crypto Pay API).

Курс задаётся в монетах за доллар (`COINS_PER_USD`), счёт выставляется
в USDT — он к доллару привязан, и цена в приложении совпадает с тем, что
человек увидит в кошельке.

Про подтверждение оплаты. У Crypto Pay есть вебхук, но здесь выбран
опрос: вебхук требует публичного адреса, отдельной настройки в кабинете
и проверки подписи — а опрос по списку своих счетов даёт тот же
результат и не ломается при переезде домена. Счета живут в той же
таблице `invoices`, что и рублёвые, и той же воркер-сверкой добиваются.

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

log = logging.getLogger("rassylka.cryptobot")

TIMEOUT = aiohttp.ClientTimeout(total=20)


def ready() -> bool:
    return bool(config.CRYPTO_TOKEN)


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


async def _call(method: str, payload: dict | None = None) -> dict:
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            f"{config.CRYPTO_API}/api/{method}",
            headers={"Crypto-Pay-API-Token": config.CRYPTO_TOKEN},
            json=payload or {},
        ) as answer:
            body = await answer.json()
    if not body.get("ok"):
        raise RuntimeError(f"CryptoBot: {body.get('error')}")
    return body["result"]


async def create(user_id: int, coins: int) -> dict:
    """Выставить счёт в USDT. Возвращает {'url': ..., 'id': ...}."""
    if not ready():
        raise RuntimeError("CryptoBot не настроен")
    usd = price_for(coins)
    if usd is None:
        raise ValueError("столько монет так не продать")

    order_id = uuid.uuid4().hex
    result = await _call("createInvoice", {
        "asset": "USDT",
        "amount": f"{usd:.2f}",
        "description": texts.pack_title(coins),
        "payload": json.dumps({"user_id": user_id, "coins": coins,
                               "order": order_id}),
        "allow_comments": False,
        "allow_anonymous": False,
    })

    invoice_id = str(result.get("invoice_id") or "")
    url = result.get("bot_invoice_url") or result.get("pay_url")
    if not invoice_id or not url:
        raise RuntimeError("CryptoBot не вернул ссылку на оплату")

    await db.open_invoice(
        f"crypto:{invoice_id}", order_id, user_id, coins, usd,
        provider="crypto",
    )
    log.info("счёт CryptoBot: %s монет за $%s, человек %s", coins, usd, user_id)
    return {"url": url, "id": invoice_id}


async def reconcile(bot) -> int:
    """Проверить свои висящие счета и начислить оплаченные."""
    if not ready():
        return 0
    pending = [
        row for row in await db.pending_invoices(max_age=86400)
        if str(row.get("provider") or "") == "crypto"
    ]
    if not pending:
        return 0

    ids = ",".join(row["transaction_id"].split(":", 1)[1] for row in pending)
    try:
        result = await _call("getInvoices", {"invoice_ids": ids})
    except Exception as error:
        log.debug("опрос счетов CryptoBot: %s", error)
        return 0

    done = 0
    for item in result.get("items", []):
        key = f"crypto:{item.get('invoice_id')}"
        status = str(item.get("status") or "")
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
    # два одновременных опроса иначе начислили бы дважды.
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
    log.info("оплата криптой: $%s → %s монет, человек %s",
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
