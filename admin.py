"""Панель владельца в боте: /admin — настройка главного меню.

Что здесь можно, и почему сделано именно так.

**Приветствие.** Владелец присылает боту любое сообщение — текст,
фото, гифку, видео, кружок, стикер, с премиум-эмодзи и разметкой, — и
оно становится приветствием бота. Мы **не разбираем** это сообщение на
части и не пересобираем его заново: запоминаем, где оно лежит, и на
`/start` копируем через `copy_message`.

Так сделано намеренно. Разбор и пересборка теряют половину оформления:
премиум-эмодзи требуют доступа к их документам, у медиа свои
`file_id`, у цитат и спойлеров — свои сущности. Копирование переносит
сообщение целиком и ничего не скачивает: файл уже лежит у Telegram.

Обратная сторона одна: **исходное сообщение нельзя удалять** из чата с
ботом — копировать станет нечего. Бот это замечает и честно
возвращается к обычному приветствию, а владельцу пишет, что случилось.

**Кнопки.** Списком строк «Текст | куда». «Куда» — ссылка или короткое
слово: app, tariffs, help, support. Это проще любого конструктора и не
требует держать в голове состояние: одно сообщение — весь набор кнопок.

**Баннер в приложении.** Строка заголовка и текст, показываются вверху
профиля. Медиа там нет: мини-апп рисует свою вёрстку, и картинка из
Telegram в неё не переносится.
"""

from __future__ import annotations

import json
import logging

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import keyboards
import texts

log = logging.getLogger("rassylka.admin")

router = Router(name="admin")

#: Ключи настроек. Собраны здесь, чтобы «сбросить всё» не забыло ни один.
KEYS = (
    "menu_chat_id",
    "menu_msg_id",
    "menu_buttons",
    "app_banner_title",
    "app_banner_text",
)


class Setup(StatesGroup):
    greeting = State()
    buttons = State()
    banner = State()


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


# Весь роутер — только для владельцев. Фильтр стоит на самом роутере, а
# не на каждом хендлере: забыть его на одном обработчике проще простого,
# а цена ошибки — чужой человек правит меню бота.
router.message.filter(F.from_user.id.func(_is_admin))
router.callback_query.filter(F.from_user.id.func(_is_admin))


def panel() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="✏️ Приветствие", callback_data="a:greeting")
    builder.button(text="🔘 Кнопки меню", callback_data="a:buttons")
    builder.button(text="📱 Баннер в приложении", callback_data="a:banner")
    builder.button(text="👀 Предпросмотр", callback_data="a:preview")
    builder.button(text="♻️ Сбросить оформление", callback_data="a:reset")
    builder.adjust(2, 1, 2)
    return builder


async def _state_text() -> str:
    saved = await db.settings_all()
    greeting = "своё сообщение" if saved.get("menu_msg_id") else "по умолчанию"
    buttons = saved.get("menu_buttons")
    count = len(json.loads(buttons)) if buttons else 0
    banner = "задан" if saved.get("app_banner_title") else "нет"
    return (
        "🛠 <b>Оформление бота</b>\n\n"
        f"Приветствие: <b>{greeting}</b>\n"
        f"Своих кнопок: <b>{count}</b>\n"
        f"Баннер в приложении: <b>{banner}</b>\n\n"
        "Всё, что здесь меняется, видят люди в боте и в приложении."
    )


@router.message(Command("admin"))
async def open_panel(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await _state_text(), reply_markup=panel().as_markup())


@router.callback_query(F.data == "a:home")
async def back_to_panel(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(
        await _state_text(), reply_markup=panel().as_markup()
    )
    await callback.answer()


def _cancel() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="← Отмена", callback_data="a:home")
    return builder


# --- приветствие -------------------------------------------------------


@router.callback_query(F.data == "a:greeting")
async def ask_greeting(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.greeting)
    await callback.message.edit_text(
        "✏️ <b>Приветствие бота</b>\n\n"
        "Пришлите следующим сообщением то, что должны видеть люди по "
        "/start. Подойдёт что угодно: текст, фото, гифка, видео, кружок, "
        "стикер — с премиум-эмодзи, жирным, ссылками и цитатами.\n\n"
        "Сообщение уйдёт людям <b>ровно в том виде</b>, в котором вы его "
        "пришлёте: бот его копирует, а не пересобирает.\n\n"
        "⚠️ Не удаляйте это сообщение из нашего чата — копировать будет "
        "нечего, и бот вернётся к обычному приветствию.",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


@router.message(Setup.greeting)
async def save_greeting(message: Message, state: FSMContext) -> None:
    await db.set_setting("menu_chat_id", str(message.chat.id))
    await db.set_setting("menu_msg_id", str(message.message_id))
    await state.clear()
    log.info("приветствие обновлено владельцем %s", message.from_user.id)
    await message.answer(
        "✅ Приветствие сохранено. Вот как его увидят люди:",
        reply_markup=_cancel().as_markup(),
    )
    await preview_start(message.bot, message.chat.id)


# --- кнопки ------------------------------------------------------------


@router.callback_query(F.data == "a:buttons")
async def ask_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.buttons)
    await callback.message.edit_text(
        "🔘 <b>Кнопки главного меню</b>\n\n"
        "Пришлите список — по кнопке в строке, в формате "
        "<code>Текст | куда</code>:\n\n"
        "<code>Наш канал | https://t.me/канал\n"
        "Открыть приложение | app\n"
        "Тарифы | tariffs\n"
        "Как это работает | help\n"
        "Поддержка | support</code>\n\n"
        "Вместо ссылки можно писать короткие слова: <code>app</code> — "
        "мини-апп, <code>tariffs</code> — цены, <code>help</code> — "
        "справка, <code>support</code> — поддержка.\n\n"
        "Кнопки документов бот добавит сам: их требует банк, и убрать "
        "их нельзя.\n\n"
        "Чтобы вернуть кнопки по умолчанию, пришлите <code>-</code>.",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


@router.message(Setup.buttons, F.text)
async def save_buttons(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    if raw == "-":
        await db.set_setting("menu_buttons", None)
        await state.clear()
        await message.answer(
            "♻️ Кнопки вернулись к обычным.", reply_markup=panel().as_markup()
        )
        return

    buttons, bad = [], []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        text, sep, target = line.partition("|")
        text, target = text.strip(), target.strip()
        if not sep or not text or not target:
            bad.append(line)
            continue
        if target in {"app", "tariffs", "help", "support"}:
            buttons.append({"text": text, "action": target})
        elif target.startswith(("https://", "http://", "tg://")):
            buttons.append({"text": text, "url": target})
        else:
            bad.append(line)

    if not buttons:
        await message.answer(
            "Ни одной кнопки не разобрал. Формат: <code>Текст | ссылка</code>, "
            "по кнопке в строке.",
            reply_markup=_cancel().as_markup(),
        )
        return

    await db.set_setting("menu_buttons", json.dumps(buttons, ensure_ascii=False))
    await state.clear()
    log.info("кнопки меню обновлены: %s штук", len(buttons))

    note = f"✅ Сохранено кнопок: {len(buttons)}."
    if bad:
        # Строки, которые не разобрались, не проглатываем молча: человек
        # думает, что кнопка есть, а её нет.
        note += "\n\n⚠️ Не понял строки:\n" + "\n".join(f"• {b}" for b in bad)
    await message.answer(note, reply_markup=panel().as_markup())


# --- баннер в приложении ----------------------------------------------


@router.callback_query(F.data == "a:banner")
async def ask_banner(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.banner)
    await callback.message.edit_text(
        "📱 <b>Баннер в приложении</b>\n\n"
        "Показывается вверху раздела «Профиль». Пришлите двумя строками:\n\n"
        "<code>Заголовок\n"
        "Текст помельче под ним</code>\n\n"
        "Медиа в баннере нет: приложение рисует свою вёрстку, картинка "
        "из Telegram в неё не переносится.\n\n"
        "Чтобы убрать баннер, пришлите <code>-</code>.",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


@router.message(Setup.banner, F.text)
async def save_banner(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    await state.clear()
    if raw == "-":
        await db.set_setting("app_banner_title", None)
        await db.set_setting("app_banner_text", None)
        await message.answer(
            "♻️ Баннер убран.", reply_markup=panel().as_markup()
        )
        return

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    await db.set_setting("app_banner_title", lines[0][:80])
    await db.set_setting(
        "app_banner_text", " ".join(lines[1:])[:300] if len(lines) > 1 else ""
    )
    await message.answer(
        "✅ Баннер сохранён — откройте приложение и обновите его.",
        reply_markup=panel().as_markup(),
    )


# --- предпросмотр и сброс ---------------------------------------------


async def preview_start(bot, chat_id: int) -> None:
    """Показать приветствие так, как его увидит обычный человек."""
    import handlers

    await handlers.send_start(bot, chat_id, None, preview=True)


@router.callback_query(F.data == "a:preview")
async def preview(callback: CallbackQuery) -> None:
    await callback.answer("Показываю")
    await preview_start(callback.bot, callback.message.chat.id)


@router.callback_query(F.data == "a:reset")
async def reset(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    for key in KEYS:
        await db.set_setting(key, None)
    log.info("оформление сброшено владельцем %s", callback.from_user.id)
    await callback.message.edit_text(
        "♻️ Оформление сброшено к обычному.\n\n" + await _state_text(),
        reply_markup=panel().as_markup(),
    )
    await callback.answer()
