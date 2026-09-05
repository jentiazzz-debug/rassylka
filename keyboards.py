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


def main_menu() -> InlineKeyboardMarkup:
    """Главное меню.

    Документы стоят отдельными кнопками, а не спрятаны за командой: их
    должно быть видно сразу, не листая переписку и не зная, что есть
    /terms. Проверяющему из банка это первое, что нужно найти, а
    обычному человеку — единственный способ прочитать условия до оплаты.
    """
    builder = InlineKeyboardBuilder()
    if config.webapp_ready():
        builder.button(
            text="🚀 Открыть приложение",
            web_app=WebAppInfo(url=config.WEBAPP_URL),
        )
    if config.SUPPORT_URL:
        builder.button(text="💬 Поддержка", url=config.SUPPORT_URL)

    if config.WEBAPP_URL:
        base = config.WEBAPP_URL.rstrip("/")
        builder.button(text="📄 Соглашение", url=f"{base}/terms")
        builder.button(text="🔒 Конфиденциальность", url=f"{base}/privacy")
        builder.button(text="💳 Тарифы", url=f"{base}/tariffs")
        builder.button(text="🛟 Поддержка и документы", url=f"{base}/support")

    builder.button(text="❓ Как это работает", callback_data="m:help")

    # Две узкие кнопки документов в ряд, остальное — по одной: адреса
    # длинные, и в один столбец список выходит на пол-экрана.
    if config.WEBAPP_URL:
        rows = [1, 1, 2, 2, 1] if config.SUPPORT_URL else [1, 2, 2, 1]
        if not config.webapp_ready():
            rows = rows[1:]
        builder.adjust(*rows)
    else:
        builder.adjust(1)
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
