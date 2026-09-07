"""Оплата рублями через Platega: счёт, callback, сверка статуса.

Как ходит платёж:

1. Мини-апп просит счёт — мы зовём ``POST /v2/transaction/process`` и
   получаем ссылку на платёжную форму.
2. Приложение открывает ссылку у человека.
3. Platega присылает нам callback: ``POST`` на PLATEGA_CALLBACK_PATH с
   заголовками ``X-MerchantId`` и ``X-Secret`` и телом со статусом.
4. На CONFIRMED начисляем монеты.

Про подлинность callback. Подписи у Platega нет — она подтверждает себя
теми же заголовками, что и мы её: присылает наш ``X-Secret``. Поэтому:

* секрет сверяется постоянным по времени сравнением, а не ``==``:
  обычное сравнение строк выходит на первом несовпавшем байте, и по
  времени ответа секрет подбирается посимвольно;
* сумма и монеты берутся **из нашей записи о счёте**, а не из тела
  callback. В теле они тоже приходят, но это данные снаружи — если
  доверять им, то подделанный callback начислит сколько попросит;
* повторный callback по тому же счёту не начисляет второй раз: Platega
  повторяет доставку до трёх раз, если мы не ответили за минуту.

Ответить нужно 200 и быстро — иначе будут повторы.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
import uuid

import aiohttp

import config
import db
import texts

log = logging.getLogger("rassylka.platega")

#: Сколько ждём ответа Platega. Дольше нет смысла: человек в это время
#: смотрит на крутилку в приложении.
TIMEOUT = aiohttp.ClientTimeout(total=20)


def _headers() -> dict:
    return {
        "X-MerchantId": config.PLATEGA_MERCHANT,
        "X-Secret": config.PLATEGA_SECRET,
        "Content-Type": "application/json",
    }


def price_of(coins: int) -> float | None:
    """Цена готовой пачки в рублях. None — такой пачки нет."""
    for pack in config.RUB_PACKS:
        if pack["coins"] == coins:
            return pack["rub"]
    return None


def min_coins() -> int:
    """Меньше этого рублями не продать: у эквайринга свой минимум."""
    from math import ceil

    return max(config.COIN_MIN, ceil(config.RUB_MIN / config.RUB_PER_COIN))


def price_for(coins: int) -> float | None:
    """Цена любого количества монет. None — столько продать нельзя.

    Готовая пачка идёт по своей цене: в ней заложена скидка, и считать
    её по базовому курсу значило бы отменить скидку у того, кто ввёл то
    же число руками. Всё остальное — по базовому курсу `RUB_PER_COIN`, в
    который комиссия эквайринга уже заложена.
    """
    exact = price_of(coins)
    if exact is not None:
        return exact
    if not (min_coins() <= coins <= config.COIN_MAX):
        return None
    return round(coins * config.RUB_PER_COIN, 2)


async def create(user_id: int, coins: int, username: str | None) -> dict:
    """Выставить счёт. Возвращает {'url': ..., 'id': ...}."""
    if not config.platega_ready():
        raise RuntimeError("Platega не настроена")
    rub = price_for(coins)
    if rub is None:
        raise ValueError("нет такой пачки монет")

    # Свой номер заказа. По нему мы потом узнаём платёж в callback, если
    # вдруг не сойдётся id транзакции.
    order_id = uuid.uuid4().hex
    payload = {
        "paymentDetails": {"amount": rub, "currency": "RUB"},
        "description": texts.pack_title(coins),
        "orderId": order_id,
        "payload": json.dumps(
            {"user_id": user_id, "coins": coins}, ensure_ascii=False
        ),
        # userId просит сама Platega — он нужен её антифроду.
        "metadata": {
            "userId": str(user_id),
            "userName": f"@{username}" if username else str(user_id),
        },
    }
    if config.WEBAPP_URL:
        payload["return"] = f"{config.WEBAPP_URL}/paid"
        payload["failedUrl"] = f"{config.WEBAPP_URL}/paid?failed=1"

    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.post(
            f"{config.PLATEGA_API}/v2/transaction/process",
            headers=_headers(),
            json=payload,
        ) as answer:
            body = await answer.text()
            if answer.status != 200:
                log.error("Platega %s: %s", answer.status, body[:400])
                raise RuntimeError(f"Platega ответила {answer.status}")
            data = json.loads(body)

    url = data.get("url")
    transaction_id = data.get("transactionId") or ""
    if not url:
        log.error("Platega не вернула ссылку: %s", body[:400])
        raise RuntimeError("Platega не вернула ссылку на оплату")

    # Записываем счёт до того, как человек пойдёт платить. Начисление
    # потом опирается на эту запись, а не на то, что придёт снаружи.
    await db.open_invoice(
        transaction_id or order_id, order_id, user_id, coins, rub
    )
    log.info(
        "счёт Platega: %s монет за %s ₽, человек %s, транзакция %s",
        coins, rub, user_id, transaction_id or order_id,
    )
    return {"url": url, "id": transaction_id or order_id}


def _same(got: str, mine: str) -> bool:
    """Постоянное по времени сравнение.

    Обычное `==` выходит на первом несовпавшем байте, и по времени
    ответа секрет подбирается посимвольно.

    Сравниваются байты, а не строки: compare_digest на строках требует
    ASCII и на не-ASCII бросает TypeError. Заголовок приходит снаружи, и
    прислать в нём кириллицу может кто угодно — на строках это уронило
    бы обработчик в 500, а Platega по 500 повторяет доставку.
    """
    return hmac.compare_digest(
        (got or "").encode("utf-8", "surrogatepass"),
        (mine or "").encode("utf-8"),
    )


def secret_ok(headers) -> bool:
    """Наш ли это секрет в заголовках callback."""
    if not config.platega_ready():
        return False
    return (
        _same(headers.get("X-Secret"), config.PLATEGA_SECRET)
        and _same(headers.get("X-MerchantId"), config.PLATEGA_MERCHANT)
    )


async def apply(body: dict, bot) -> str:
    """Разобрать callback и начислить монеты. Возвращает, что случилось."""
    transaction_id = str(body.get("id") or "")
    status = str(body.get("status") or "").upper()
    if not transaction_id:
        return "нет id"

    invoice = await db.invoice(transaction_id)
    if invoice is None:
        # Бывает, если id транзакции в callback не тот, что вернулся при
        # создании: ищем по нашему номеру заказа из payload.
        try:
            payload = json.loads(body.get("payload") or "{}")
        except json.JSONDecodeError:
            payload = {}
        log.warning(
            "callback по неизвестному счёту %s (payload %s)",
            transaction_id, payload,
        )
        return "счёт неизвестен"

    if invoice["status"] != "pending":
        # Platega повторяет доставку до трёх раз, если мы не ответили за
        # минуту. Второй раз начислять нельзя.
        return "уже обработан"

    if status != "CONFIRMED":
        await db.close_invoice(transaction_id, status.lower() or "canceled")
        return f"статус {status or 'пустой'}"

    # Сумма и монеты — из нашей записи, а не из тела callback: тело
    # приходит снаружи, и доверять его числам нельзя.
    coins = int(invoice["coins"])
    user_id = int(invoice["user_id"])
    await db.close_invoice(transaction_id, "paid")
    await db.record_payment(
        f"platega:{transaction_id}",
        user_id,
        0,
        coins,
        f"rub:{invoice['rub']}",
    )
    balance = await db.add_coins(
        user_id, coins, f"покупка за {invoice['rub']:.0f} ₽"
    )
    log.info(
        "оплата рублями: %s ₽ → %s монет, человек %s",
        invoice["rub"], coins, user_id,
    )

    if bot is not None:
        try:
            await bot.send_message(
                user_id, texts.rub_payment_done(coins, invoice["rub"], balance)
            )
        except Exception as error:
            log.debug("уведомление об оплате не ушло: %s", error)

    import payments

    await payments.reward_referrer(bot, user_id, coins)
    return "начислено"


async def check(transaction_id: str) -> dict | None:
    """Спросить у Platega статус счёта — на случай потерянного callback."""
    if not config.platega_ready():
        return None
    async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
        async with session.get(
            f"{config.PLATEGA_API}/transaction/{transaction_id}",
            headers=_headers(),
        ) as answer:
            if answer.status != 200:
                return None
            return await answer.json()


async def reconcile(bot) -> int:
    """Добить счета, по которым callback не дошёл.

    Callback могут не доставить: сеть, передеплой, упавший процесс.
    Человек в этом случае заплатил, а монет не увидел — и пойдёт в
    поддержку. Поэтому висящие счета переспрашиваются у Platega.
    """
    if not config.platega_ready():
        return 0
    done = 0
    for invoice in await db.pending_invoices(max_age=86400):
        if str(invoice.get("provider") or "platega") != "platega":
            continue
        try:
            state = await check(invoice["transaction_id"])
        except Exception as error:
            log.debug("сверка счёта %s: %s", invoice["transaction_id"], error)
            continue
        if not state:
            continue
        status = str(state.get("status") or "").upper()
        if status == "CONFIRMED":
            await apply({"id": invoice["transaction_id"], "status": status}, bot)
            done += 1
        elif status in {"CANCELED", "CHARGEBACKED"}:
            await db.close_invoice(invoice["transaction_id"], status.lower())
    if done:
        log.info("досчитано счетов после потерянных callback: %s", done)
    return done


async def worker(bot) -> None:
    """Раз в несколько минут сверять зависшие счета."""
    while True:
        await asyncio.sleep(300)
        try:
            await reconcile(bot)
        except Exception:
            log.exception("сверка счетов Platega сорвалась")
        try:
            import cryptobot

            await cryptobot.reconcile(bot)
        except Exception:
            log.exception("сверка счетов CryptoBot сорвалась")
        try:
            import xrocket

            await xrocket.reconcile(bot)
        except Exception:
            log.exception("сверка счетов xRocket сорвалась")
