"""Оплата подписки звёздами Telegram.

Звёзды — единственный способ взять деньги прямо в мини-аппе, не уводя
человека на сторонний сайт: для цифровых услуг Telegram другого и не
разрешает. Технически это обычные платежи Bot API, только валюта
`XTR` и без токена платёжного провайдера.

Как это ходит:

1. Мини-апп просит ссылку на счёт — `create_invoice_link`.
2. Открывает её у себя: `tg.openInvoice(link)`.
3. Telegram спрашивает подтверждение у бота — `pre_checkout_query`.
   Ответить надо за 10 секунд, иначе платёж отменится.
4. Приходит `successful_payment` — вот здесь и продлевается подписка.

Продление вешается на шаг 4, а не на ответ мини-аппа: `openInvoice`
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
PREFIX = "sub"


def plan_by_days(days: int) -> dict | None:
    for plan in config.PLANS:
        if plan["days"] == days:
            return plan
    return None


def payload_for(days: int) -> str:
    return f"{PREFIX}:{days}"


def days_from(payload: str) -> int:
    """Сколько дней куплено. 0 — payload не наш или испорчен."""
    parts = (payload or "").split(":")
    if len(parts) != 2 or parts[0] != PREFIX or not parts[1].isdigit():
        return 0
    return int(parts[1])


async def invoice_link(bot, days: int) -> str:
    """Ссылка на счёт для мини-аппа."""
    plan = plan_by_days(days)
    if plan is None:
        raise ValueError("нет такого тарифа")
    return await bot.create_invoice_link(
        title=texts.plan_title(plan["days"]),
        description=texts.plan_description(plan["days"]),
        payload=payload_for(plan["days"]),
        # Для звёзд провайдер не нужен, а валюта всегда XTR и цена
        # указывается прямо в звёздах — без множителя на сотые, как в
        # обычных валютах.
        provider_token="",
        currency="XTR",
        prices=[
            LabeledPrice(
                label=texts.plan_title(plan["days"]), amount=plan["stars"]
            )
        ],
    )


@router.pre_checkout_query()
async def confirm(query: PreCheckoutQuery) -> None:
    """Подтвердить платёж. Ответить надо за 10 секунд.

    Отказываем только по неизвестному тарифу: всё остальное проверять
    уже поздно, а неотвеченный запрос выглядит для человека как
    зависший платёж.
    """
    days = days_from(query.invoice_payload)
    if not days or plan_by_days(days) is None:
        await query.answer(ok=False, error_message="Этот тариф больше не действует.")
        log.warning("отказ по счёту: payload %r", query.invoice_payload)
        return
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message) -> None:
    """Платёж прошёл — продлить подписку и начислить монеты."""
    payment: SuccessfulPayment = message.successful_payment
    user = message.from_user
    days = days_from(payment.invoice_payload)
    if not days or user is None:
        log.error("непонятный платёж: %r", payment.invoice_payload)
        return

    stars = int(payment.total_amount or 0)
    fresh = await db.record_payment(
        payment.telegram_payment_charge_id,
        user.id,
        stars,
        days,
        payment.invoice_payload,
    )
    if not fresh:
        # Telegram присылает успешный платёж повторно, если бот не
        # ответил вовремя. Второй раз продлевать нельзя.
        return

    until = await db.grant_paid(user.id, days)
    coins = await db.add_coins(user.id, stars * config.COINS_PER_STAR)
    log.info(
        "оплата: %s звёзд, %s дней, человек %s, до %s", stars, days, user.id, until
    )
    await message.answer(texts.payment_done(days, stars, until, coins))
