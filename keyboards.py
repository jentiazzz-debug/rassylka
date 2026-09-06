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
        action = item.get("action")
        if action == "app":
            if config.webapp_ready():
                builder.button(text=text, web_app=WebAppInfo(url=config.WEBAPP_URL))
                custom_rows += 1
            continue
        if action == "support":
            if config.SUPPORT_URL:
                builder.button(text=text, url=config.SUPPORT_URL)
                custom_rows += 1
            continue
        if action in ACTIONS:
            builder.button(text=text, callback_data=ACTIONS[action])
            custom_rows += 1
            continue
        url = str(item.get("url") or "").strip()
        if url.startswith(("https://", "http://", "tg://")):
            builder.button(text=text, url=url)
            custom_rows += 1

    if not custom:
        if config.webapp_ready():
            builder.button(
                text="🚀 Открыть приложение",
                web_app=WebAppInfo(url=config.WEBAPP_URL),
            )

    # Поддержка и документы — вторым рядом, рядом друг с другом.
    support_row = 0
    if config.SUPPORT_URL and not custom:
        builder.button(text="💬 Поддержка", url=config.SUPPORT_URL)
        support_row += 1
    # Кнопка документов есть всегда, даже поверх своих кнопок владельца:
    # её требует банк, и убрать её из меню нельзя.
    builder.button(text="📄 Документы", callback_data="m:docs")
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
