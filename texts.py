"""Тексты бота. Всё в одном месте, чтобы править их без правки логики."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from html import escape

import config
import db


def plural(number: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 день, 2 дня, 5 дней."""
    tail = abs(number) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many


def when(stamp: int, offset_hours: int = 3) -> str:
    """Дата по часовому поясу владельца, а не по UTC сервера."""
    if not stamp:
        return "—"
    moment = datetime.fromtimestamp(stamp, timezone(timedelta(hours=offset_hours)))
    return moment.strftime("%d.%m.%Y %H:%M")


def subscription_line(subscription: db.Subscription) -> str:
    """Одна строка про подписку — та же, что человек видит в приложении."""
    days = subscription.days_left
    if subscription.kind == "paid":
        return (
            f"💎 Подписка активна, осталось {days} "
            f"{plural(days, 'день', 'дня', 'дней')}"
        )
    if subscription.kind == "trial":
        return (
            f"🎁 Пробный период: осталось {days} "
            f"{plural(days, 'день', 'дня', 'дней')}"
        )
    return "⌛️ Бесплатный период закончился"


def start(name: str | None, subscription: db.Subscription) -> str:
    hello = escape((name or "").strip()) or "Привет"
    trial = config.TRIAL_DAYS
    return (
        f"👋 <b>{hello}, это бот для рассылок.</b>\n\n"
        "Подключаете свой аккаунт по номеру телефона — и рассылаете от "
        "своего имени: в личные сообщения, в группы и в чаты, где этот "
        "аккаунт уже есть.\n\n"
        f"{subscription_line(subscription)}\n\n"
        f"Первые {trial} {plural(trial, 'день', 'дня', 'дней')} — "
        "бесплатно. На бесплатном тарифе в конце каждого сообщения "
        "рассылки дописывается строка о том, каким ботом она сделана.\n\n"
        "Всё управление — в приложении: нажмите кнопку ниже."
    )


#: Что видит человек, если приложение ещё не настроено. Обычному
#: пользователю это ни о чём не говорит, поэтому текст — про поддержку, а
#: подробности уходят в лог владельцу.
NO_WEBAPP = (
    "⚙️ Приложение пока недоступно — идут работы.\n\n"
    "Загляните чуть позже или напишите в поддержку."
)

HELP = (
    "<b>Как это работает</b>\n\n"
    "1. Открываете приложение и подключаете свой аккаунт по номеру "
    "телефона. Telegram пришлёт код в чат «Telegram» — введите его в "
    "приложении. Если на аккаунте стоит облачный пароль, спросим и его.\n"
    "2. Аккаунт остаётся подключённым: увидеть и отозвать эту сессию "
    "можно в Telegram → Настройки → Устройства.\n"
    "3. Рассылка идёт от лица этого аккаунта.\n\n"
    "<b>О лимитах.</b> Telegram считает частые сообщения незнакомым "
    "людям спамом и выдаёт за это ограничения самому аккаунту. Поэтому "
    "рассылка идёт с паузами и дневным лимитом, а свежий аккаунт лучше "
    "разгонять постепенно. Рассылайте тем, кто ждёт от вас сообщений: "
    "жалобы получателей — главная причина блокировок.\n\n"
    "Вопросы — в поддержку."
)

SUPPORT_MISSING = (
    "Поддержка пока не настроена. Напишите владельцу бота напрямую."
)


# --- оплата -----------------------------------------------------------


def pack_title(coins: int) -> str:
    return f"{coins} {config.COIN_NAME}"


def pack_description(coins: int) -> str:
    return (
        f"{coins} монет на счёт. Ими оплачивается подписка на рассылки; "
        "монеты не сгорают."
    )


def payment_done(coins: int, stars: int, balance: int) -> str:
    return (
        "✅ <b>Оплата прошла</b>\n\n"
        f"Начислено {coins} {escape(config.COIN_NAME)} за {stars} ⭐️.\n"
        f"Баланс: {coins_line(balance)}.\n\n"
        "Подписка покупается за монеты — в приложении, раздел «Профиль»."
    )


def rub_payment_done(coins: int, rub: float, balance: int) -> str:
    return (
        "✅ <b>Оплата прошла</b>\n\n"
        f"Начислено {coins} {escape(config.COIN_NAME)} за {rub:.0f} ₽.\n"
        f"Баланс: {coins_line(balance)}.\n\n"
        "Подписка покупается за монеты — в приложении, раздел «Профиль»."
    )


def subscribed(days: int, price: int, until: int, balance: int) -> str:
    return (
        "✅ <b>Подписка активна</b>\n\n"
        f"Продлили на {days} {plural(days, 'день', 'дня', 'дней')}, "
        f"до {when(until)}.\n"
        f"Списано {price} {escape(config.COIN_NAME)}, осталось {balance}.\n\n"
        "Подпись о боте в сообщениях рассылки больше не добавляется."
    )


def referral_share(share: int, balance: int) -> str:
    return (
        f"💎 <b>+{share} {escape(config.COIN_NAME)}</b>\n\n"
        "Приглашённый вами человек пополнил баланс — вам капнула доля.\n"
        f"Ваш баланс: {coins_line(balance)}."
    )


def referral_joined(coins: int, balance: int) -> str:
    return (
        "👋 <b>По вашей ссылке пришёл человек</b>\n\n"
        f"Начислено {coins} {escape(config.COIN_NAME)}. "
        f"Баланс: {coins_line(balance)}.\n\n"
        "Дальше вам будет капать доля с его пополнений."
    )


def plan_title(days: int) -> str:
    if days == 1:
        return "Подписка на день"
    if days >= 30:
        return "Подписка на месяц"
    return f"Подписка на {days} {plural(days, 'день', 'дня', 'дней')}"


def plan_description(days: int) -> str:
    return (
        f"Рассылки без ограничений {days} "
        f"{plural(days, 'день', 'дня', 'дней')}. "
        "Подпись о боте в конце сообщений не добавляется."
    )


def invite(link: str, stats: dict) -> str:
    return (
        "🤝 <b>Приглашайте — получайте монеты</b>\n\n"
        f"Ваша ссылка:\n{escape(link)}\n\n"
        f"За каждого, кто придёт по ней: <b>{config.REF_COINS} "
        f"{escape(config.COIN_NAME)}</b>. Дальше — "
        f"<b>{config.REF_PERCENT}%</b> с каждого его пополнения.\n\n"
        f"Пришло по ссылке: <b>{stats['invited']}</b>, "
        f"заработано: <b>{stats['earned']} {escape(config.COIN_NAME)}</b>"
    )


def documents(base: str) -> str:
    base = base.rstrip("/")
    return (
        "📄 <b>Документы и тарифы</b>\n\n"
        f'• <a href="{escape(base)}/terms">Пользовательское соглашение</a>\n'
        f'• <a href="{escape(base)}/privacy">Политика конфиденциальности</a>\n'
        f'• <a href="{escape(base)}/tariffs">Тарифы и что входит в услугу</a>\n'
        f'• <a href="{escape(base)}/support">Поддержка и реквизиты</a>'
    )


def coins_line(coins: int) -> str:
    """Баланс внутренней валюты одной строкой."""
    return f"{coins} {config.COIN_NAME}"


# --- уведомления о рассылках ------------------------------------------

#: Пометка на рассылке, упёршейся в дневной лимит. Сравнивается по
#: значению, чтобы не написать человеку одно и то же двадцать раз подряд.
NOTE_DAILY = "дневной лимит аккаунта"

CAMPAIGN_EXPIRED = (
    "⌛️ <b>Рассылки поставлены на паузу</b>\n\n"
    "Бесплатный период закончился. Аккаунты остались подключёнными, "
    "и рассылки — тоже: включим подписку, и они продолжат с того же "
    "места.\n\nНапишите в поддержку."
)


def campaign_account_dead(campaign) -> str:
    return (
        f"⚠️ <b>Рассылка «{escape(campaign.title)}» остановлена</b>\n\n"
        "Аккаунт больше не в сети: сессию отозвали в Telegram либо "
        "аккаунт заблокирован. Откройте приложение и подключите его "
        "заново — рассылка продолжит с того места, где встала."
    )


def material_gone(campaign) -> str:
    return (
        f"⚠️ <b>Рассылка «{escape(campaign.title)}» на паузе</b>\n\n"
        "Сообщение-материал пропало из «Избранного» аккаунта — отправлять "
        "нечего. Положите его туда снова, обновите список в приложении и "
        "выберите материал заново."
    )


def campaign_empty(campaign) -> str:
    return (
        f"⚠️ <b>Рассылка «{escape(campaign.title)}» на паузе</b>\n\n"
        "В списке чатов не осталось ни одного. Если это папка — "
        "обновите список чатов в приложении."
    )


def daily_limit(account: str) -> str:
    return (
        f"🛡 <b>Дневной лимит достигнут</b>\n\n"
        f"Аккаунт {escape(account)} отправил столько сообщений, сколько "
        "мы считаем безопасным за сутки. Рассылки продолжатся сами, "
        "когда счётчик освободится.\n\n"
        "Это не ошибка, а защита: Telegram блокирует аккаунты именно за "
        "превышение таких объёмов."
    )


def flood_wait(account: str, seconds: int) -> str:
    hours = seconds // 3600
    when = f"{hours} ч" if hours else f"{seconds // 60} мин"
    return (
        f"⏳ <b>Telegram притормозил аккаунт</b>\n\n"
        f"{escape(account)} просят молчать {when}. Рассылки с этого "
        "аккаунта продолжатся автоматически.\n\n"
        "Если это повторяется — увеличьте интервал."
    )


def peer_flood(account: str) -> str:
    return (
        f"🚫 <b>Telegram ограничил аккаунт {escape(account)}</b>\n\n"
        "Рассылки с него остановлены на сутки. Это ограничение за спам: "
        "следующий шаг Telegram — блокировка номера, поэтому продолжать "
        "нельзя.\n\n"
        "Что делать: увеличить интервал, убрать из списка чаты, где вас "
        "не ждут, и не рассылать людям, которые об этом не просили — "
        "жалобы получателей и есть главная причина таких ограничений."
    )


def stats(numbers: dict[str, int]) -> str:
    return (
        "📊 <b>Сводка</b>\n\n"
        f"Людей всего: <b>{numbers.get('users', 0)}</b>\n"
        f"Заходили за сутки: <b>{numbers.get('active_day', 0)}</b>\n"
        f"На пробном: <b>{numbers.get('on_trial', 0)}</b>\n"
        f"С оплатой: <b>{numbers.get('paid', 0)}</b>\n\n"
        f"Аккаунтов подключено: <b>{numbers.get('accounts', 0)}</b>, "
        f"из них живых: <b>{numbers.get('accounts_ok', 0)}</b>"
    )
