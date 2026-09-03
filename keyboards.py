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
    builder = InlineKeyboardBuilder()
    if config.webapp_ready():
        builder.button(
            text="🚀 Открыть приложение",
            web_app=WebAppInfo(url=config.WEBAPP_URL),
        )
    if config.SUPPORT_URL:
        builder.button(text="💬 Поддержка", url=config.SUPPORT_URL)
    builder.button(text="❓ Как это работает", callback_data="m:help")
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
