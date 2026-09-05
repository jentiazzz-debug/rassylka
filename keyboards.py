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
    """Главное меню.

    Документы стоят отдельными кнопками, а не спрятаны за командой: их
    должно быть видно сразу, не листая переписку и не зная, что есть
    /terms. Проверяющему из банка это первое, что нужно найти, а
    обычному человеку — единственный способ прочитать условия до оплаты.
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
        if config.SUPPORT_URL:
            builder.button(text="💬 Поддержка", url=config.SUPPORT_URL)

    # Документы добавляются всегда, даже поверх своих кнопок: их
    # требует банк, и убрать их из меню владелец не может.
    if config.WEBAPP_URL:
        base = config.WEBAPP_URL.rstrip("/")
        builder.button(text="📄 Соглашение", url=f"{base}/terms")
        builder.button(text="🔒 Конфиденциальность", url=f"{base}/privacy")
        builder.button(text="🛟 Поддержка и документы", url=f"{base}/support")

    # Тарифы — не ссылкой, а сообщением прямо в чате: цены человек
    # смотрит перед оплатой, и уводить его за ними из Telegram незачем.
    # Страница /tariffs при этом остаётся — банку нужен адрес, который
    # открывается без Telegram.
    builder.button(text="💳 Тарифы", callback_data="m:tariffs")
    builder.button(text="❓ Как это работает", callback_data="m:help")

    # Две узкие кнопки документов в ряд, остальное — по одной: адреса
    # длинные, и в один столбец список выходит на пол-экрана.
    if custom:
        # Свои кнопки — по одной в строке: тексты у них произвольные, и
        # две длинные подписи рядом не помещаются.
        tail = [2, 1, 2] if config.WEBAPP_URL else [2]
        builder.adjust(*([1] * custom_rows + tail))
    elif config.WEBAPP_URL:
        rows = [1, 1, 2, 1, 2] if config.SUPPORT_URL else [1, 2, 1, 2]
        if not config.webapp_ready():
            rows = rows[1:]
        builder.adjust(*rows)
    else:
        builder.adjust(1, 2)
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
