"""Монеты, подписка и оплата звёздами Telegram.

Деньги ходят по двум дорогам, и обе сходятся в монетах:

    звёзды  →  монеты  →  подписка
    друзья  →  монеты  ↗

Звёзды — единственный способ взять деньги прямо в мини-аппе, не уводя
человека на сторонний сайт: для цифровых услуг Telegram другого и не
разрешает. Технически это обычные платежи Bot API, только валюта `XTR`
и без токена платёжного провайдера.

Как ходит платёж:

1. Мини-апп просит ссылку на счёт — `create_invoice_link`.
2. Открывает её у себя: `tg.openInvoice(link)`.
3. Telegram спрашивает подтверждение у бота — `pre_checkout_query`.
   Ответить надо за 10 секунд, иначе платёж отменится.
4. Приходит `successful_payment` — вот здесь и начисляются монеты.

Начисление висит на шаге 4, а не на ответе мини-аппа: `openInvoice`
возвращает статус в браузер, и поверить ему нельзя — это клиент, его
ответ подделывается. Настоящее подтверждение приходит боту от Telegram.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import (
    LabeledPrice,
    Message,
    PreCheckoutQuery,
    SuccessfulPayment,
)

import config
import db
import texts

log = logging.getLogger("rassylka.payments")

router = Router(name="payments")

#: Метка в payload, чтобы отличать свои счета от чужих.
PREFIX = "coins"


def plan_by_days(days: int) -> dict | None:
    """Тариф подписки. В `stars` у него лежит цена в монетах."""
    for plan in config.PLANS:
        if plan["days"] == days:
            return plan
    return None


def plan_price(days: int) -> int | None:
    plan = plan_by_days(days)
    return plan["stars"] if plan else None


def pack_price(coins: int) -> int:
    """Во сколько звёзд обойдётся пачка монет."""
    return coins * config.STARS_PER_COIN


def payload_for(coins: int) -> str:
    return f"{PREFIX}:{coins}"


def coins_from(payload: str) -> int:
    """Сколько монет куплено. 0 — payload не наш или испорчен."""
    parts = (payload or "").split(":")
    if len(parts) != 2 or parts[0] != PREFIX or not parts[1].isdigit():
        return 0
    return int(parts[1])


async def invoice_link(bot, coins: int) -> str:
    """Ссылка на счёт для мини-аппа."""
    if coins not in config.COIN_PACKS:
        raise ValueError("нет такой пачки монет")
    stars = pack_price(coins)
    return await bot.create_invoice_link(
        title=texts.pack_title(coins),
        description=texts.pack_description(coins),
        payload=payload_for(coins),
        # Для звёзд провайдер не нужен, а валюта всегда XTR и цена
        # указывается прямо в звёздах — без множителя на сотые, как в
        # обычных валютах.
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label=texts.pack_title(coins), amount=stars)],
    )


async def buy_subscription(user_id: int, days: int) -> tuple[bool, str]:
    """Купить подписку за монеты. Возвращает (получилось, что сказать)."""
    price = plan_price(days)
    if price is None:
        return False, "Такого тарифа нет."
    if not await db.spend_coins(user_id, price, f"подписка на {days} дн."):
        have = await db.coins_of(user_id)
        return False, (
            f"Не хватает монет: нужно {price} {config.COIN_NAME}, "
            f"у вас {have}. Пополните баланс звёздами."
        )
    await db.grant_paid(user_id, days)
    return True, ""


@router.pre_checkout_query()
async def confirm(query: PreCheckoutQuery) -> None:
    """Подтвердить платёж. Ответить надо за 10 секунд.

    Отказываем только по непонятному счёту: всё остальное проверять уже
    поздно, а неотвеченный запрос выглядит для человека как зависший
    платёж.
    """
    coins = coins_from(query.invoice_payload)
    if not coins or coins not in config.COIN_PACKS:
        await query.answer(
            ok=False, error_message="Этот счёт больше не действует."
        )
        log.warning("отказ по счёту: payload %r", query.invoice_payload)
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    """Платёж прошёл — начислить монеты покупателю и долю пригласившему."""
    payment: SuccessfulPayment = message.successful_payment
    user = message.from_user
    coins = coins_from(payment.invoice_payload)
    if not coins or user is None:
        log.error("непонятный платёж: %r", payment.invoice_payload)
        return

    stars = int(payment.total_amount or 0)
    fresh = await db.record_payment(
        payment.telegram_payment_charge_id,
        user.id,
        stars,
        coins,
        payment.invoice_payload,
    )
    if not fresh:
        # Telegram присылает успешный платёж повторно, если бот не
        # ответил вовремя. Второй раз начислять нельзя.
        return

    balance = await db.add_coins(user.id, coins, f"покупка за {stars} звёзд")
    log.info("оплата: %s звёзд → %s монет, человек %s", stars, coins, user.id)
    await message.answer(texts.payment_done(coins, stars, balance))

    await reward_referrer(message.bot, user.id, coins)


async def reward_referrer(bot, user_id: int, coins: int) -> None:
    """Доля пригласившему с покупки приглашённого."""
    if not config.REF_PERCENT:
        return
    inviter = await db.referrer_of(user_id)
    if not inviter:
        return
    share = coins * config.REF_PERCENT // 100
    if share <= 0:
        return
    balance = await db.add_coins(inviter, share, "приглашённый пополнил баланс")
    try:
        await bot.send_message(inviter, texts.referral_share(share, balance))
    except Exception as error:
        log.debug("уведомление о доле не ушло: %s", error)
