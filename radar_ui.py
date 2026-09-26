"""Управление радаром командами бота.

Почему командами, а не в мини-аппе, где живёт всё остальное. Первая
фаза радара — замер: человек включает его на сутки и смотрит, сколько
заказов вообще приносят выбранные чаты. Рисовать под замер экраны рано:
половину настроек после первых суток выбросят, а другой половины
сейчас не угадать. Команды дают то же самое за вечер и правятся так же
быстро, как меняется представление о том, что здесь вообще нужно.

Разбор аргументов нарочно грубый — по разделителю. Диалогов с
состоянием здесь нет: в мини-аппе им место найдётся, а в чате они
превратились бы в тот самый мастер настройки, из которого нельзя выйти.
"""

from __future__ import annotations

import logging
import time

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

import chats as chats_scan
import classify
import config
import db
import texts

log = logging.getLogger("rassylka.radar_ui")

router = Router(name="radar")

HELP = (
    "📡 <b>Радар заказов</b>\n\n"
    "Слушает чаты вашим аккаунтом и приносит в личку то, что похоже на "
    "заказ. Ничего никуда не пишет — только читает и отбирает.\n\n"
    "<b>Как запустить</b>\n"
    "1. <code>/radar_new Боты | делаю телеграм-ботов на python и "
    "aiogram, мини-аппы, интеграции с оплатой</code>\n"
    "2. <code>/radar_add 1 заказ</code> — добавит чаты, в названии "
    "которых есть «заказ»\n"
    "3. <code>/radar_words 1 бот, telegram, aiogram, автоматизац</code>\n"
    "4. <code>/radar_on 1</code>\n\n"
    "<b>Остальное</b>\n"
    "<code>/radar</code> — список и счётчики\n"
    "<code>/leads</code> — последние находки\n"
    "<code>/leads_skip</code> — что классификатор отсеял\n"
    "<code>/radar_stats</code> — сводка за сутки\n"
    "<code>/radar_off 1</code>, <code>/radar_drop 1</code>\n\n"
    "Профиль во втором шаге — главное поле: по нему модель решает, ваш "
    "это заказ или чужой. Пишите своими словами и подробно."
)


async def _own(message: Message) -> int | None:
    user = message.from_user
    return user.id if user else None


def _arg(command: CommandObject) -> str:
    return (command.args or "").strip()


def _split(raw: str) -> tuple[str, str]:
    """Первое слово и остаток. Номер радара всегда идёт первым."""
    parts = raw.split(maxsplit=1)
    return (parts[0] if parts else ""), (parts[1].strip() if len(parts) > 1 else "")


async def _radar_of(user_id: int, raw: str) -> tuple[db.Radar | None, str]:
    number, rest = _split(raw)
    if not number.isdigit():
        return None, rest
    return await db.radar(user_id, int(number)), rest


@router.message(Command("radar"))
async def radar_list(message: Message) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    found = await db.radars(user_id)
    if not found:
        await message.answer(HELP, disable_web_page_preview=True)
        return

    lines = ["📡 <b>Ваши радары</b>", ""]
    for radar in found:
        count = await db.count_radar_chats(radar.id)
        lines.append(texts.radar_line(radar, count))
        if radar.note:
            lines.append(f"    <i>{radar.note}</i>")
        lines.append("")
    if not classify.ready():
        lines.append(
            "⚠️ Ключ классификатора не задан: радар собирает сообщения "
            "по ключевым словам, но смысл в них не разбирает — в ленту "
            "попадёт заметно больше лишнего."
        )
    await message.answer("\n".join(lines), disable_web_page_preview=True)


@router.message(Command("radar_new"))
async def radar_new(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return

    raw = _arg(command)
    if "|" not in raw:
        await message.answer(
            "Нужно название и профиль через <code>|</code>:\n\n"
            "<code>/radar_new Боты | делаю телеграм-ботов на python, "
            "мини-аппы, интеграции с оплатой</code>"
        )
        return

    title, profile = (part.strip() for part in raw.split("|", 1))
    if len(profile) < 20:
        # Короткий профиль — главная причина мусорной ленты: по трём
        # словам модель не отличит ваш заказ от чужого.
        await message.answer(
            "Профиль слишком короткий. Опишите, что вы делаете, "
            "подробно — от этого напрямую зависит, сколько мусора "
            "окажется в ленте."
        )
        return

    if await db.count_radars(user_id) >= config.MAX_RADARS:
        await message.answer(
            f"Больше {config.MAX_RADARS} радаров на аккаунт нельзя."
        )
        return

    accounts = [a for a in await db.accounts(user_id) if a.status == "ok"]
    if not accounts:
        await message.answer(
            "Сначала подключите аккаунт в приложении — радару нужно, от "
            "чьего имени читать чаты."
        )
        return

    radar = await db.create_radar(user_id, accounts[0].id, title, profile)
    await message.answer(
        f"📡 Радар <b>{radar.id}</b> создан на аккаунте "
        f"{accounts[0].title}.\n\n"
        f"Дальше: <code>/radar_add {radar.id} заказ</code> — добавить "
        f"чаты, потом <code>/radar_on {radar.id}</code>."
    )


@router.message(Command("radar_add"))
async def radar_add(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return

    radar, needle = await _radar_of(user_id, _arg(command))
    if radar is None:
        await message.answer("Укажите номер радара: <code>/radar_add 1 заказ</code>")
        return
    if not needle:
        await message.answer(
            "Укажите часть названия чата: <code>/radar_add 1 заказ</code>"
        )
        return

    known = await db.chats(radar.account_id)
    if not known:
        note = await message.answer("Читаю список чатов аккаунта…")
        try:
            await chats_scan.scan(user_id, radar.account_id)
        except Exception as error:
            log.warning("скан чатов для радара сорвался: %s", error)
            await note.edit_text(
                "Не получилось прочитать список чатов. Откройте "
                "приложение и обновите чаты там."
            )
            return
        known = await db.chats(radar.account_id)

    low = needle.lower()
    # Каналы отсеиваем: заказы пишут в группах, а канал — это вещание,
    # и слушать его радаром значит платить за разбор чужой рекламы.
    picked = [
        chat for chat in known
        if low in (chat.title or "").lower() and not chat.broadcast
    ]
    if not picked:
        await message.answer(
            f"Среди чатов аккаунта нет групп со словом «{needle}» в "
            "названии."
        )
        return

    room = config.MAX_RADAR_CHATS - await db.count_radar_chats(radar.id)
    if room <= 0:
        await message.answer(
            f"В радаре уже {config.MAX_RADAR_CHATS} чатов — это потолок."
        )
        return

    picked = picked[:room]
    added = await db.add_radar_chats(
        radar.id,
        [{"chat_id": c.chat_id, "title": c.title} for c in picked],
    )
    listed = "\n".join(f"• {c.title}" for c in picked[:15])
    tail = f"\n…и ещё {len(picked) - 15}" if len(picked) > 15 else ""
    await message.answer(
        f"Добавлено чатов: <b>{added}</b>\n\n{listed}{tail}\n\n"
        f"Запустить: <code>/radar_on {radar.id}</code>"
    )


@router.message(Command("radar_words"))
async def radar_words(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    radar, words = await _radar_of(user_id, _arg(command))
    if radar is None:
        await message.answer(
            "Укажите номер радара: <code>/radar_words 1 бот, aiogram</code>"
        )
        return

    await db.edit_radar(user_id, radar.id, keywords=words)
    if words:
        await message.answer(
            f"Ключевые слова радара {radar.id}: <code>{words}</code>\n\n"
            "До разбора моделью теперь доходят только сообщения, где "
            "есть хотя бы одно из них."
        )
    else:
        await message.answer(
            f"Ключевые слова радара {radar.id} убраны: до разбора будет "
            "доходить всё, что похоже на заказ вообще. Ленты это не "
            "испортит, но разбор обойдётся дороже."
        )


@router.message(Command("radar_on"))
async def radar_on(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    radar, _ = await _radar_of(user_id, _arg(command))
    if radar is None:
        await message.answer("Укажите номер радара: <code>/radar_on 1</code>")
        return
    if not await db.count_radar_chats(radar.id):
        await message.answer(
            f"В радаре нет чатов. Добавьте: <code>/radar_add {radar.id} "
            "заказ</code>"
        )
        return

    await db.set_radar_status(radar.id, "running", "")
    await message.answer(
        f"🟢 Радар <b>{radar.title}</b> запущен. Находки будут приходить "
        "сюда же.\n\nЧерез сутки посмотрите <code>/radar_stats</code> — "
        "по нему и станет понятно, стоит ли автоматизировать ответы или "
        "хватит уведомлений."
    )


@router.message(Command("radar_off"))
async def radar_off(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    radar, _ = await _radar_of(user_id, _arg(command))
    if radar is None:
        await message.answer("Укажите номер радара: <code>/radar_off 1</code>")
        return
    await db.set_radar_status(radar.id, "stopped", "остановлен вручную")
    await message.answer(f"⏸ Радар <b>{radar.title}</b> остановлен.")


@router.message(Command("radar_drop"))
async def radar_drop(message: Message, command: CommandObject) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    radar, _ = await _radar_of(user_id, _arg(command))
    if radar is None:
        await message.answer("Укажите номер радара: <code>/radar_drop 1</code>")
        return
    await db.delete_radar(user_id, radar.id)
    await message.answer(
        f"Радар <b>{radar.title}</b> удалён. Найденные заказы остались — "
        "они в <code>/leads</code>."
    )


@router.message(Command("leads"))
async def leads_list(message: Message) -> None:
    await _leads(message, "order", "Заказов пока нет.")


@router.message(Command("leads_skip"))
async def leads_skipped(message: Message) -> None:
    await _leads(
        message,
        "skip",
        "Классификатор пока ничего не отсеял.",
        note=(
            "Это то, что радар посмотрел и решил не показывать. Если "
            "здесь окажется настоящий заказ — расширьте профиль радара "
            "или опустите планку оценки."
        ),
    )


async def _leads(message: Message, verdict: str, empty: str,
                 note: str = "") -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    found = await db.leads(user_id, limit=10, verdict=verdict)
    if not found:
        await message.answer(empty)
        return
    if note:
        await message.answer(note)
    for lead in found:
        # Ссылку на сообщение здесь не собираем: для неё нужен аккаунт и
        # чат из базы, а в ленте прошлых находок это десяток лишних
        # запросов ради строки, по которой чаще всего уже не пойдут.
        await message.answer(
            texts.lead_card(lead, None), disable_web_page_preview=True
        )


@router.message(Command("radar_stats"))
async def radar_stats(message: Message) -> None:
    user_id = await _own(message)
    if user_id is None:
        return
    hours = 24
    since = int(time.time()) - hours * 3600
    stats = await db.radar_stats(user_id, since)
    usage = await db.radar_usage_today(user_id)
    await message.answer(texts.radar_stats(stats, usage, hours))
