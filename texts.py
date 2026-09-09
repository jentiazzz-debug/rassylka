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
        "Всё управление — в приложении: нажмите кнопку ниже.\n\n"
        "Условия, политика и тарифы — под кнопкой «Документы»."
        + review_line()
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

GREETING_LOST = (
    "⚠️ <b>Своё приветствие пропало</b>\n\n"
    "Сообщение, которое вы задали приветствием, удалено из нашего чата — "
    "копировать нечего. Бот вернулся к обычному тексту.\n\n"
    "Задайте приветствие заново: /admin"
)

SUPPORT = (
    "🛟 <b>Поддержка</b>\n\n"
    "Откройте приложение, раздел «Поддержка» — там частые вопросы и "
    "форма обращения. К обращению можно приложить скриншот или запись "
    "экрана: с ними разбираются в разы быстрее.\n\n"
    "У каждого обращения есть номер и переписка, ответ приходит сюда, "
    "в этот чат. Так ни одно не теряется — в отличие от сообщения, "
    "написанного среди уведомлений о рассылках."
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


def crypto_payment_done(coins: int, usd: float, balance: int) -> str:
    return (
        "✅ <b>Оплата прошла</b>\n\n"
        f"Начислено {coins} {escape(config.COIN_NAME)} за ${usd:.2f}.\n"
        f"Баланс: {coins_line(balance)}.\n\n"
        "Подписка покупается за монеты — в приложении."
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


def plan_period(days: int) -> str:
    if days == 1:
        return "1 день"
    if days >= 30:
        return "30 дней"
    return f"{days} {plural(days, 'день', 'дня', 'дней')}"


def plan_name(days: int) -> str:
    if days == 1:
        return "День"
    if days >= 30:
        return "Месяц"
    if days >= 7:
        return "Неделя"
    return plan_period(days)


def plan_audience(index: int, total: int) -> str:
    """Кому какой тариф. Определяется местом в списке, а не числом дней:
    тарифы задаются в .env и могут быть любыми."""
    if total == 1:
        return "для любых задач"
    if index == 0:
        return "попробовать на своей базе, разовая рассылка"
    if index == total - 1:
        return "постоянная работа, несколько аккаунтов"
    return "регулярные рассылки в несколько чатов"


def plan_saving(plan: dict, base: dict) -> int:
    """Насколько тариф выгоднее самого короткого, в процентах."""
    if plan is base or not base["days"]:
        return 0
    per_day = plan["stars"] / plan["days"]
    base_per_day = base["stars"] / base["days"]
    if base_per_day <= 0:
        return 0
    return max(0, round((1 - per_day / base_per_day) * 100))


def price_hint(coins: int) -> str:
    """Во что монеты обходятся деньгами.

    Цену в монетах человек не чувствует: «200 Slx» ни о чём не говорит,
    пока рядом нет привычной суммы. Считается по тем же курсам, по
    которым идёт пополнение, — отдельных чисел здесь не заводится.
    """
    parts = []
    if config.platega_ready() and config.RUB_PER_COIN:
        parts.append(f"≈ {round(coins * config.RUB_PER_COIN)} ₽")
    if config.STARS_PER_COIN:
        parts.append(f"{coins * config.STARS_PER_COIN} ⭐️")
    return " или ".join(parts)


def tariffs() -> str:
    """Тарифы сообщением в чате.

    Собирается из config, как и страница /tariffs: цена в двух местах
    однажды разъедется, а разъехавшаяся цена — это спор с клиентом и
    вопрос от банка. Источник один.
    """
    plans = sorted(config.PLANS, key=lambda p: p["days"])
    base = plans[0] if plans else None

    blocks = []
    for index, plan in enumerate(plans):
        saving = plan_saving(plan, base)
        name, period = plan_name(plan["days"]), plan_period(plan["days"])
        # У однодневного тарифа название и срок совпадают — «День — 1
        # день» читается как оговорка, а не как заголовок.
        head = f"<b>{name}</b>" if name == period else f"<b>{name}</b> · {period}"
        if saving >= 5:
            head += f"  ·  выгоднее на {saving}%"
        per_day = round(plan["stars"] / plan["days"])
        hint = price_hint(plan["stars"])
        blocks.append(
            f"{head}\n"
            f"<b>{plan['stars']} {escape(config.COIN_NAME)}</b>"
            + (f" · {hint}" if hint else "")
            + f"\n{per_day} {escape(config.COIN_NAME)} в день · "
            + plan_audience(index, len(plans))
        )

    included = [
        "рассылка по группам, каналам и личным сообщениям",
        f"до {config.MAX_ACCOUNTS} подключённых аккаунтов",
        f"до {config.MAX_VARIANTS} вариантов сообщения в одной рассылке",
        "медиа и оформление из «Избранного»",
        "журнал отправок по каждому чату",
        "поддержка",
    ]

    conditions = [
        "подписка начинается сразу после оплаты",
        "продление прибавляется к остатку, а не обнуляет его",
        "автосписаний нет — продлевать нужно вручную",
        "монеты не сгорают и не имеют срока действия",
    ]

    ways = [
        "• Telegram Stars: "
        + ", ".join(
            f"{pack['coins']} за {pack['stars']} ⭐️"
            for pack in config.COIN_PACKS
        )
    ]
    if config.platega_ready() and config.RUB_PACKS:
        ways.append(
            "• Карта или СБП: "
            + ", ".join(
                f"{pack['coins']} за {pack['rub']:.0f} ₽"
                for pack in config.RUB_PACKS
            )
        )
    if config.CRYPTO_TOKEN and config.CRYPTO_PACKS:
        ways.append(
            "• Криптовалютой: "
            + ", ".join(
                f"{pack['coins']} за ${pack['usd']:.0f}"
                for pack in config.CRYPTO_PACKS
            )
        )

    trial = config.TRIAL_DAYS
    text = (
        "💳 <b>Тарифы</b>\n\n"
        "Подписка снимает подпись о сервисе в конце отправляемых "
        "сообщений и оставляет рассылки работать после пробного "
        f"периода. Платят монетами ({escape(config.COIN_NAME)}), монеты "
        "покупаются отдельно.\n\n"
        + "\n\n".join(blocks)
        + "\n\n<b>Что входит в любой тариф</b>\n"
        + "\n".join(f"• {line}" for line in included)
        + "\n\n<b>Условия</b>\n"
        + "\n".join(f"• {line}" for line in conditions)
        + f"\n• бесплатно первые {trial} "
        + plural(trial, "день", "дня", "дней")
        + ": все функции, но в конце каждого сообщения дописывается "
        "строка о сервисе"
        + "\n\n<b>Как пополнить монеты</b>\n"
        + "\n".join(ways)
        + "\n\n<b>Ограничения</b> — действуют всегда, оплатой не "
        "снимаются:\n"
        f"• не чаще одного сообщения в {config.MIN_INTERVAL} секунд\n"
        f"• не больше {config.DAILY_LIMIT} сообщений в сутки с аккаунта\n"
        "Они снижают риск ограничений со стороны Telegram для вашего "
        "аккаунта."
    )
    if config.WEBAPP_URL:
        base_url = config.WEBAPP_URL.rstrip("/")
        text += (
            f'\n\n<a href="{escape(base_url)}/tariffs">Полные условия и '
            f'возврат</a> · <a href="{escape(base_url)}/terms">Соглашение</a>'
        )
    return text + review_line()


def review_line() -> str:
    """Строка с кодовым словом. Пусто — переменная не задана.

    Нужна на время согласования: проверяющий по ней убеждается, что
    смотрит именно тот сервис, который подавали. Убирается очисткой
    REVIEW_CODE, без правки текстов.
    """
    if not config.REVIEW_CODE:
        return ""
    return f"\n\nКодовое слово: <code>{escape(config.REVIEW_CODE)}</code>"


def documents(base: str) -> str:
    base = base.rstrip("/")
    return (
        "📄 <b>Документы и тарифы</b>\n\n"
        f'• <a href="{escape(base)}/terms">Пользовательское соглашение</a>\n'
        f'• <a href="{escape(base)}/privacy">Политика конфиденциальности</a>\n'
        f'• <a href="{escape(base)}/tariffs">Тарифы и что входит в услугу</a>\n'
        f'• <a href="{escape(base)}/support">Поддержка и документы</a>'
        + review_line()
    )


def from_support(coins: int, days: int) -> str:
    parts = []
    if coins:
        parts.append(f"{coins:+d} {escape(config.COIN_NAME)}")
    if days:
        parts.append(f"подписка продлена на {days} дн.")
    return (
        "🛟 <b>Поддержка внесла изменения</b>\n\n"
        + ", ".join(parts)
        + "\n\nЕсли что-то осталось не так — напишите нам."
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


def watch_account_dead(watch) -> str:
    return (
        f"⚠️ <b>Автокомментарии в «{escape(watch.title)}» остановлены</b>\n\n"
        "Аккаунт больше не в сети: сессию отозвали в Telegram либо "
        "аккаунт заблокирован. Откройте приложение и подключите его "
        "заново."
    )


def watch_material_gone(watch) -> str:
    return (
        f"⚠️ <b>Автокомментарии в «{escape(watch.title)}» на паузе</b>\n\n"
        "Сообщение-материал пропало из «Избранного» аккаунта — отправлять "
        "нечего. Положите его туда снова, обновите список в приложении и "
        "выберите материал заново."
    )


def banned(reason: str) -> str:
    return (
        "🚫 <b>Доступ закрыт</b>\n\n"
        + (f"Причина: {escape(reason)}.\n\n" if reason else "")
        + "Рассылки и автокомментарии остановлены. Купленное никуда не "
        "делось и останется на счету, если доступ вернут."
    )


def ticket_closed(ticket_id: int) -> str:
    return (
        f"✅ <b>Обращение #{ticket_id} закрыто</b>\n\n"
        "Если вопрос остался — заведите новое в приложении, раздел "
        "«Поддержка». Переписка по закрытому остаётся видна там же."
    )


def watch_too_expensive(watch, stars: int) -> str:
    return (
        f"💫 <b>Автокомментарии в «{escape(watch.title)}» на паузе</b>\n\n"
        f"Этот чат берёт <b>{stars}</b> ⭐️ за сообщение — больше, чем вы "
        "разрешили тратить.\n\n"
        "Звёзды списываются с самого подключённого аккаунта, а не с "
        "баланса в боте, поэтому решение за вами: поднимите потолок в "
        "приложении, раздел «Профиль», — и наблюдение продолжится."
    )


def watch_stuck(watch, reason: str) -> str:
    return (
        f"⚠️ <b>Автокомментарии в «{escape(watch.title)}» на паузе</b>\n\n"
        f"Telegram ответил: {escape(reason)}.\n\n"
        "Чаще всего это значит, что аккаунт не состоит в группе "
        "обсуждений канала или у канала её нет вовсе. Зайдите в канал с "
        "этого аккаунта, откройте комментарии под любым постом и "
        "вступите в обсуждение."
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
