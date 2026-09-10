"""Клавиатуры. Главное меню — две кнопки: приложение и поддержка.

Кнопка приложения появляется только при https-адресе в WEBAPP_URL.
Telegram не примет в web_app ни http, ни localhost, и это не ошибка
запроса: клиент просто не откроет ничего по нажатию. Кнопка, которая
молча не работает, хуже отсутствующей — поэтому её просто нет, а в лог
на старте уходит объяснение.

Схема callback_data: <code>m:действие</code>.
"""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardMarkup,
    MenuButtonCommands,
    MenuButtonWebApp,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config


#: Короткие слова вместо ссылок в настройке кнопок: владелец пишет
#: «Тарифы | tariffs», а не ищет, какой у кнопки callback_data.
ACTIONS = {
    "tariffs": "m:tariffs",
    "help": "m:help",
}

#: Премиум-эмодзи и цвет на кнопках главного меню. Telegram принимает их
#: полями icon_custom_emoji_id и style — старые клиенты про них не знают
#: и просто покажут кнопку без значка, поэтому подпись у каждой кнопки
#: осмысленная сама по себе.
APP_EMOJI = "5870994129244131212"
DOCS_EMOJI = "5870528606328852614"
SUPPORT_EMOJI = "5260535596941582167"


#: Цвета кнопок. Telegram знает три: primary — голубая, success —
#: зелёная, danger — красная. Четвёртого нет: серая кнопка — это кнопка
#: без цвета, поэтому «серая» тут даёт None, а не строку.
STYLES = {
    "primary": "primary",
    "голубая": "primary",
    "синяя": "primary",
    "success": "success",
    "зелёная": "success",
    "зеленая": "success",
    "danger": "danger",
    "красная": "danger",
    "серая": None,
    "обычная": None,
}


def parse_buttons(raw: str) -> tuple[list[dict], list[str]]:
    """Разобрать список кнопок из текста владельца.

    Формат строки: <code>Текст | куда | эмодзи | цвет</code>, и всё после
    первых двух полей необязательно. Один формат и для меню, и для
    рассылки — владельцу не нужно помнить два.

    Возвращает разобранные кнопки и строки, которые понять не удалось.
    Непонятые не проглатываем: иначе владелец уверен, что кнопка есть, а
    её нет.
    """
    items, bad = [], []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        text, target = (parts + ["", ""])[:2]
        if not text or not target:
            bad.append(line)
            continue

        item = {"text": text[:64]}
        if target in ACTIONS or target == "app" or target == "support":
            item["action"] = target
        elif target.startswith(("https://", "http://", "tg://")):
            item["url"] = target
        else:
            bad.append(line)
            continue

        for extra in parts[2:]:
            if extra.isdigit():
                item["emoji"] = extra
            elif extra.lower() in STYLES:
                style = STYLES[extra.lower()]
                if style:
                    item["style"] = style
            elif extra:
                bad.append(line)
                break
        else:
            items.append(item)
            continue
        # break выше — строку уже записали в непонятые.
    return items, bad


def _decorate(item: dict) -> dict:
    """Оформление кнопки — премиум-эмодзи и цвет — в вид aiogram.

    Старые клиенты этих полей не знают и покажут кнопку без значка и
    цвета, поэтому подпись у кнопки должна быть осмысленной сама по
    себе. Ломаться от этого ничего не ломается.
    """
    extra = {}
    if item.get("emoji"):
        extra["icon_custom_emoji_id"] = str(item["emoji"])
    if item.get("style"):
        extra["style"] = item["style"]
    return extra


def cast_markup(items: list[dict] | None) -> InlineKeyboardMarkup | None:
    """Кнопки под сообщением рассылки владельца — по одной в ряд.

    Тут рассылает бот, а не подключённый аккаунт, поэтому инлайн-кнопки
    возможны: MTProto не даёт обычному аккаунту прикрепить клавиатуру,
    Bot API боту — даёт.
    """
    if not items:
        return None
    builder = InlineKeyboardBuilder()
    rows = 0
    for item in items:
        text = str(item.get("text") or "").strip()[:64]
        if not text:
            continue
        extra = _decorate(item)
        action = item.get("action")
        if action == "app":
            if not config.webapp_ready():
                continue
            builder.button(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL), **extra)
        elif action == "support":
            if not config.webapp_ready():
                continue
            builder.button(
                text=text, web_app=WebAppInfo(url=config.WEBAPP_URL), **extra
            )
        elif action in ACTIONS:
            builder.button(text=text, callback_data=ACTIONS[action], **extra)
        else:
            url = str(item.get("url") or "").strip()
            if not url.startswith(("https://", "http://", "tg://")):
                continue
            builder.button(text=text, url=url, **extra)
        rows += 1
    if not rows:
        return None
    builder.adjust(*([1] * rows))
    return builder.as_markup()


def main_menu(custom: list[dict] | None = None) -> InlineKeyboardMarkup:
    """Главное меню: приложение, поддержка и одна кнопка «Документы».

    Документы собраны под одну кнопку, а не разложены пятью подряд.
    Пять служебных кнопок занимали больше места, чем всё остальное меню,
    и главное — «Открыть приложение» тонуло среди них.

    Из-под кнопки они открываются в один тап, и там же лежат тарифы,
    соглашение, политика и поддержка — то, что просил показывать банк.
    Прямые пути тоже остались: команды /terms, /tariffs и /support
    работают без всякого меню.
    """
    builder = InlineKeyboardBuilder()
    custom_rows = 0

    # Свои кнопки идут первыми: владелец ставит их ради того, чтобы их
    # увидели, а не ради того, чтобы они прятались под служебными.
    for item in custom or []:
        text = str(item.get("text") or "").strip()[:64]
        if not text:
            continue
        extra = _decorate(item)
        action = item.get("action")
        if action == "app":
            if config.webapp_ready():
                builder.button(
                    text=text, web_app=WebAppInfo(url=config.WEBAPP_URL), **extra
                )
                custom_rows += 1
            continue
        if action == "support":
            # Ведёт в приложение, а не на чей-то профиль: поддержка — это
            # раздел с обращениями. Слово в настройке осталось прежним,
            # чтобы у владельца не сломались уже заданные кнопки.
            if config.webapp_ready():
                builder.button(
                    text=text, web_app=WebAppInfo(url=config.WEBAPP_URL), **extra
                )
                custom_rows += 1
            continue
        if action in ACTIONS:
            builder.button(text=text, callback_data=ACTIONS[action], **extra)
            custom_rows += 1
            continue
        url = str(item.get("url") or "").strip()
        if url.startswith(("https://", "http://", "tg://")):
            builder.button(text=text, url=url, **extra)
            custom_rows += 1

    if not custom:
        if config.webapp_ready():
            builder.button(
                text="Открыть приложение",
                web_app=WebAppInfo(url=config.WEBAPP_URL),
                icon_custom_emoji_id=APP_EMOJI,
                style="primary",
            )

    # Поддержка и документы — вторым рядом, рядом друг с другом.
    # Поддержка ведёт в приложение, а не на чей-то профиль. Обращения
    # там превращаются в тикеты: у каждого есть номер, переписка и
    # статус, и ни одно не теряется в личке среди уведомлений. Ссылка на
    # живого человека этого не даёт — и, кроме того, привязывает сервис
    # к конкретному аккаунту, который однажды меняется.
    support_row = 0
    if config.webapp_ready() and not custom:
        builder.button(
            text="Поддержка",
            web_app=WebAppInfo(url=config.WEBAPP_URL),
            icon_custom_emoji_id=SUPPORT_EMOJI,
            style="primary",
        )
        support_row += 1
    # Кнопка документов есть всегда, даже поверх своих кнопок владельца:
    # её требует банк, и убрать её из меню нельзя.
    builder.button(
        text="Документы",
        callback_data="m:docs",
        icon_custom_emoji_id=DOCS_EMOJI,
    )
    support_row += 1

    if custom:
        builder.adjust(*([1] * custom_rows + [support_row]))
    else:
        head = [1] if config.webapp_ready() else []
        builder.adjust(*(head + [support_row]))
    return builder.as_markup()


def docs_menu() -> InlineKeyboardMarkup:
    """Что открывается под кнопкой «Документы».

    Соглашение, политика и поддержка — ссылками на страницы: банку нужен
    адрес, открывающийся без Telegram. Тарифы — сообщением в чате: цены
    смотрят перед оплатой, и уводить за ними из Telegram незачем.
    """
    builder = InlineKeyboardBuilder()
    rows = []

    if config.WEBAPP_URL:
        base = config.WEBAPP_URL.rstrip("/")
        builder.button(text="📄 Соглашение", url=f"{base}/terms")
        builder.button(text="🔒 Конфиденциальность", url=f"{base}/privacy")
        rows.append(2)
        # Страница поддержки остаётся: банк требует адрес, который
        # открывается без Telegram. Живого контакта на ней нет — есть
        # почта и порядок обращения.
        builder.button(text="🛟 Поддержка и реквизиты", url=f"{base}/support")
        rows.append(1)

    builder.button(text="💳 Тарифы", callback_data="m:tariffs")
    builder.button(text="❓ Как это работает", callback_data="m:help")
    rows.append(2)

    builder.button(text="← Назад", callback_data="m:home")
    rows.append(1)

    builder.adjust(*rows)
    return builder.as_markup()


def back() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="← Назад", callback_data="m:home")
    return builder.as_markup()


def menu_button() -> MenuButtonWebApp | MenuButtonCommands:
    """Кнопка меню у поля ввода: открывает то же приложение.

    Это второй вход в приложение, и он важнее, чем кажется: сообщение с
    инлайн-кнопкой уезжает вверх по переписке, а кнопка у поля ввода
    остаётся на месте всегда.
    """
    if config.webapp_ready():
        return MenuButtonWebApp(
            text="Приложение", web_app=WebAppInfo(url=config.WEBAPP_URL)
        )
    return MenuButtonCommands()
