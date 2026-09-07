"""Точка входа: база, веб-сервер мини-аппа, уборка входов, поллинг.

Бот и веб-сервер поднимаются в одном процессе. Это не экономия ради
экономии: незавершённый вход по номеру — это живое подключение Telethon
в памяти (phone_code_hash привязан к нему), и разнести API мини-аппа с
этим подключением по разным процессам нельзя — код из Telegram перестал
бы приниматься.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram import __version__ as aiogram_version
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand, BotCommandScopeChat

import accounts
import admin
import broadcast
import comments
import config
import crypto
import db
import handlers
import keyboards
import payments
import platega
import webapp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("rassylka")

COMMANDS = (
    ("start", "Главное меню"),
    ("help", "Как это работает"),
    ("invite", "Пригласить друга"),
    ("tariffs", "Тарифы и цены"),
    ("terms", "Документы"),
    ("support", "Поддержка"),
)

ADMIN_COMMANDS = (
    ("admin", "Оформление бота"),
    ("stats", "Сводка"),
)


async def run() -> None:
    config.check()
    await db.connect()

    # Шифрование проверяем на старте, а не при первом входе по номеру.
    # Сломанный ключ иначе всплыл бы у человека посреди подключения —
    # после того, как Telegram уже прислал ему код.
    if not crypto.ready():
        raise SystemExit(
            "Шифрование сессий не работает: без него подключать аккаунты "
            "нельзя. Проверьте, что установлена cryptography "
            "(pip install -r requirements.txt) и что SESSION_KEY — "
            "корректный ключ Fernet."
        )

    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher()
    # Платежи впереди общего роутера: у handlers последним стоит хендлер
    # на любое сообщение в личке, и сообщение об успешной оплате он бы
    # съел — подписка не продлилась бы, а деньги ушли.
    dispatcher.include_router(payments.router)
    # Админка впереди общего роутера: она ждёт от владельца обычные
    # сообщения (приветствие, список кнопок), а у handlers последним
    # стоит хендлер на любое сообщение в личке — он бы их съел.
    dispatcher.include_router(admin.router)
    dispatcher.include_router(handlers.router)

    # Счета на оплату выписывает бот, а просит их мини-апп.
    webapp.use_bot(bot)

    me = await bot.me()
    # Версии в лог не для красоты: вход по номеру ломается от версии
    # Telethon, и первое, что нужно знать при разборе такого отказа, —
    # какая из них стоит на этом сервере.
    import telethon

    log.info(
        "запущен как @%s, aiogram %s, telethon %s, python %s",
        me.username, aiogram_version, telethon.__version__,
        sys.version.split()[0],
    )

    await bot.set_my_commands(
        [BotCommand(command=c, description=d) for c, d in COMMANDS]
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await bot.set_my_commands(
                [
                    BotCommand(command=c, description=d)
                    for c, d in COMMANDS + ADMIN_COMMANDS
                ],
                scope=BotCommandScopeChat(chat_id=admin_id),
            )
        except Exception as error:  # админ мог не нажать /start
            log.warning("не смог выставить команды для %s: %s", admin_id, error)

    try:
        await bot.set_chat_menu_button(menu_button=keyboards.menu_button())
    except Exception as error:
        log.warning("кнопку меню выставить не удалось: %s", error)

    # Копившиеся за простой апдейты выбрасываем: отвечать меню на
    # вчерашние сообщения смысла нет.
    await bot.delete_webhook(drop_pending_updates=True)

    runner = await webapp.serve()
    sweeper = asyncio.create_task(accounts.sweeper(), name="logins-sweeper")
    # Движок рассылки получает бота: про упёршийся лимит и про
    # ограничение от Telegram человек должен узнать сразу в личке, а не
    # когда сам зайдёт в приложение.
    sender = asyncio.create_task(broadcast.worker(bot), name="broadcast")
    # Сверка счетов Platega: callback могут не доставить — сеть,
    # передеплой, упавший процесс. Человек в этом случае заплатил, а
    # монет не увидел, и пойдёт в поддержку.
    invoices = asyncio.create_task(platega.worker(bot), name="invoices")
    # Автокомментарии — свой воркер, а не ветка в движке рассылки: у
    # рассылки расписание, здесь событие, и общий цикл пришлось бы
    # будить с частотой самого нетерпеливого из двух.
    watcher = asyncio.create_task(comments.worker(bot), name="comments")
    try:
        await dispatcher.start_polling(bot)
    finally:
        for task in (sweeper, sender, invoices, watcher):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        await broadcast.close_all()
        # Незавершённые входы держат подключения к Telegram: закрываем их
        # руками, иначе процесс не завершается до таймаута.
        await accounts.close_all()
        await runner.cleanup()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except (KeyboardInterrupt, SystemExit) as stop:
        log.info("остановлен: %s", stop or "Ctrl+C")
