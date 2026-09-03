"""Хендлеры бота: главное меню, справка, сводка для владельца.

Вся работа с аккаунтами и рассылкой живёт в мини-аппе. Бот здесь —
входная дверь: /start, кнопка «Открыть приложение», кнопка поддержки.
Дублировать подключение аккаунта диалогом в чате смысла нет: код и
облачный пароль в переписке с ботом остаются в истории чата, а в
приложении — нет.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message

import config
import db
import keyboards
import texts

log = logging.getLogger("rassylka.handlers")

router = Router(name="menu")


async def _remember(message: Message) -> None:
    user = message.from_user
    if user is None:
        return
    await db.ensure_user(
        user.id,
        user.username,
        " ".join(part for part in (user.first_name, user.last_name) if part) or None,
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await _remember(message)
    user = message.from_user
    subscription = await db.subscription(user.id if user else 0)
    await message.answer(
        texts.start(user.first_name if user else None, subscription),
        reply_markup=keyboards.main_menu(),
    )
    if not config.webapp_ready():
        # Человеку это ни о чём не скажет, а владельцу подскажет, почему
        # в меню одна кнопка вместо двух.
        await message.answer(texts.NO_WEBAPP)


@router.message(Command("help"))
async def help_command(message: Message) -> None:
    await _remember(message)
    await message.answer(texts.HELP, reply_markup=keyboards.main_menu())


@router.message(Command("support"))
async def support_command(message: Message) -> None:
    await _remember(message)
    if not config.SUPPORT_URL:
        await message.answer(texts.SUPPORT_MISSING)
        return
    await message.answer(
        f"💬 Поддержка: {config.SUPPORT_URL}", reply_markup=keyboards.main_menu()
    )


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in config.ADMIN_IDS:
        return
    await message.answer(texts.stats(await db.stats()))


@router.callback_query(F.data == "m:help")
async def help_button(callback: CallbackQuery) -> None:
    await callback.message.edit_text(texts.HELP, reply_markup=keyboards.back())
    await callback.answer()


@router.callback_query(F.data == "m:home")
async def home_button(callback: CallbackQuery) -> None:
    user = callback.from_user
    subscription = await db.subscription(user.id)
    await callback.message.edit_text(
        texts.start(user.first_name, subscription),
        reply_markup=keyboards.main_menu(),
    )
    await callback.answer()


@router.message(F.chat.type == "private")
async def anything_else(message: Message) -> None:
    """Любое другое сообщение в личке — показать меню.

    Стоит последним в роутере: aiogram отдаёт сообщение первому
    подошедшему хендлеру, и выше этого фильтр «любое сообщение» съел бы
    все команды.
    """
    await _remember(message)
    user = message.from_user
    subscription = await db.subscription(user.id if user else 0)
    await message.answer(
        texts.start(user.first_name if user else None, subscription),
        reply_markup=keyboards.main_menu(),
    )
