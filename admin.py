"""Панель владельца в боте: /admin — настройка главного меню.

Что здесь можно, и почему сделано именно так.

**Приветствие** бывает двух видов, и выбор между ними — это выбор
между медиа и переменными; всё остальное умеют оба.

*Своё сообщение.* Владелец присылает боту что угодно — текст, фото,
гифку, видео, кружок, стикер, — и на `/start` бот копирует это через
`copy_message`. Мы **не разбираем** сообщение на части: разбор и
пересборка теряют половину оформления, а копирование переносит его
целиком и ничего не скачивает — файл уже лежит у Telegram. Обратных
сторон две: исходное сообщение **нельзя удалять** из чата с ботом
(копировать станет нечего — бот это заметит и вернётся к обычному
приветствию), и копия одинакова для всех, подставить в неё имя нельзя.

*Текст с переменными.* Хранится текстом и сущностями, поэтому в нём
работают `{name}`, `{balance}`, `{days}` и остальные — вместе с жирным,
цитатой, ссылками и премиум-эмодзи. Медиа в нём нет.

**Кнопки.** Списком строк «Текст | куда | эмодзи | цвет». «Куда» —
ссылка или короткое слово: app, tariffs, help, support. Два последних
поля необязательны: эмодзи — id премиум-значка, цвет — голубая, зелёная
или красная. Это проще любого конструктора и не требует держать в
голове состояние: одно сообщение — весь набор кнопок.

**Рассылка по пользователям бота.** Отдельная от рассылки по чатам:
здесь пишет сам бот своим людям, и поэтому здесь возможны инлайн-кнопки
— обычному аккаунту MTProto прикрепить клавиатуру не даёт.

**Баннер в приложении.** Строка заголовка и текст, показываются вверху
профиля. Медиа там нет: мини-апп рисует свою вёрстку, и картинка из
Telegram в неё не переносится.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
import db
import keyboards
import richtext
import texts

log = logging.getLogger("rassylka.admin")

router = Router(name="admin")

#: Ключи настроек. Собраны здесь, чтобы «сбросить всё» не забыло ни один.
KEYS = (
    "menu_chat_id",
    "menu_msg_id",
    "menu_text",
    "menu_entities",
    "menu_buttons",
    "app_banner_title",
    "app_banner_text",
)


class Setup(StatesGroup):
    greeting = State()
    greeting_text = State()
    buttons = State()
    banner = State()


class Cast(StatesGroup):
    """Рассылка владельца: сообщение, потом кнопки, потом подтверждение."""

    message = State()
    buttons = State()


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
    builder.button(text="📣 Рассылка по людям", callback_data="a:cast")
    builder.button(text="👀 Предпросмотр", callback_data="a:preview")
    builder.button(text="♻️ Сбросить оформление", callback_data="a:reset")
    builder.adjust(2, 2, 2)
    return builder


async def _state_text() -> str:
    saved = await db.settings_all()
    if saved.get("menu_text"):
        greeting = "текст с переменными"
    elif saved.get("menu_msg_id"):
        greeting = "своё сообщение"
    else:
        greeting = "по умолчанию"
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
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text="📎 Своё сообщение", callback_data="a:g:media")
    builder.button(text="🔤 Текст с переменными", callback_data="a:g:text")
    builder.button(text="♻️ Вернуть обычное", callback_data="a:g:off")
    builder.button(text="← Назад", callback_data="a:home")
    builder.adjust(2, 1, 1)
    await callback.message.edit_text(
        "✏️ <b>Приветствие бота</b>\n\n"
        "<b>📎 Своё сообщение</b> — пришлёте что угодно, бот скопирует это "
        "людям как есть: фото, гифка, видео, кружок, стикер, премиум-эмодзи, "
        "разметка. Одинаковое для всех — подставить имя в копию нельзя.\n\n"
        "<b>🔤 Текст с переменными</b> — то же оформление (жирный, цитата, "
        "ссылки, премиум-эмодзи), но с обращением по имени и балансом. "
        "Без медиа.",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "a:g:off")
async def greeting_off(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    for key in ("menu_chat_id", "menu_msg_id", "menu_text", "menu_entities"):
        await db.set_setting(key, None)
    await callback.message.edit_text(
        "♻️ Вернул обычное приветствие.\n\n" + await _state_text(),
        reply_markup=panel().as_markup(),
    )
    await callback.answer()


@router.callback_query(F.data == "a:g:media")
async def ask_greeting_media(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.greeting)
    await callback.message.edit_text(
        "📎 <b>Своё сообщение</b>\n\n"
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
    # Режимы взаимно исключают друг друга: send_start смотрит текст
    # первым, и оставленный текст перекрыл бы только что заданное медиа.
    await db.set_setting("menu_text", None)
    await db.set_setting("menu_entities", None)
    await state.clear()
    log.info("приветствие обновлено владельцем %s", message.from_user.id)
    await message.answer(
        "✅ Приветствие сохранено. Вот как его увидят люди:",
        reply_markup=_cancel().as_markup(),
    )
    await preview_start(message.bot, message.chat.id)


@router.callback_query(F.data == "a:g:text")
async def ask_greeting_text(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.greeting_text)
    await callback.message.edit_text(
        "🔤 <b>Текст с переменными</b>\n\n"
        "Пришлите текст приветствия — с премиум-эмодзи, жирным, курсивом, "
        "цитатой, спойлером и ссылками: всё оформление сохранится.\n\n"
        + richtext.help_text()
        + "\n\nНапример:\n"
        "<code>Привет, {name}!\nНа счету {balance} "
        + config.COIN_NAME
        + ", подписка: {tariff}.</code>",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


@router.message(Setup.greeting_text, F.text)
async def save_greeting_text(message: Message, state: FSMContext) -> None:
    await db.set_setting("menu_text", message.text)
    await db.set_setting("menu_entities", richtext.dump(message.entities))
    # См. выше: два приветствия одновременно жить не могут.
    await db.set_setting("menu_chat_id", None)
    await db.set_setting("menu_msg_id", None)
    await state.clear()
    log.info("текстовое приветствие обновлено владельцем %s", message.from_user.id)

    unknown = _unknown_variables(message.text)
    note = "✅ Сохранено. Вот как его увидят люди:"
    if unknown:
        # Опечатка в имени переменной оставляет в приветствии фигурные
        # скобки — человек увидит их как есть.
        note = (
            "✅ Сохранено, но переменные "
            + ", ".join(f"<code>{{{name}}}</code>" for name in unknown)
            + " я не знаю — они останутся в тексте как есть.\n\n"
            "Вот как его увидят люди:"
        )
    await message.answer(note, reply_markup=_cancel().as_markup())
    await preview_start(message.bot, message.chat.id)


@router.message(Setup.greeting_text)
async def greeting_text_not_text(message: Message) -> None:
    await message.answer(
        "Здесь нужен именно текст — медиа в этом режиме не показывается. "
        "Если нужна картинка, вернитесь и выберите «📎 Своё сообщение».",
        reply_markup=_cancel().as_markup(),
    )


def _unknown_variables(text: str) -> list[str]:
    """Что в фигурных скобках похоже на переменную, но ею не является."""
    import re

    found = re.findall(r"\{([a-zA-Z_]+)\}", text or "")
    return sorted({name for name in found if name not in richtext.VARIABLES})


# --- кнопки ------------------------------------------------------------


@router.callback_query(F.data == "a:buttons")
async def ask_buttons(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Setup.buttons)
    await callback.message.edit_text(
        "🔘 <b>Кнопки главного меню</b>\n\n" + _buttons_help() + "\n\n"
        "Кнопки документов бот добавит сам: их требует банк, и убрать "
        "их нельзя.\n\n"
        "Чтобы вернуть кнопки по умолчанию, пришлите <code>-</code>.",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


def _buttons_help() -> str:
    """Один и тот же формат кнопок — и для меню, и для рассылки."""
    return (
        "Пришлите список — по кнопке в строке, в формате "
        "<code>Текст | куда | эмодзи | цвет</code>:\n\n"
        "<code>Наш канал | https://t.me/канал\n"
        "Открыть приложение | app | 5870994129244131212 | голубая\n"
        "Тарифы | tariffs | | зелёная\n"
        "Поддержка | support | 5260535596941582167 | голубая</code>\n\n"
        "Вместо ссылки можно писать короткие слова: <code>app</code> — "
        "мини-апп, <code>tariffs</code> — цены, <code>help</code> — "
        "справка, <code>support</code> — поддержка.\n\n"
        "Два последних поля необязательны. <b>Эмодзи</b> — id "
        "премиум-значка (перешлите мне такой эмодзи, и я подскажу его id). "
        "<b>Цвет</b> — голубая, зелёная, красная или серая."
    )


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

    buttons, bad = keyboards.parse_buttons(raw)
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


# --- рассылка по пользователям бота -----------------------------------
#
# Это не та рассылка, которой пользуются люди: та идёт по чатам с их
# подключённых аккаунтов через MTProto. Здесь пишет сам бот своим
# пользователям — и именно поэтому здесь возможны инлайн-кнопки с цветом
# и премиум-эмодзи: обычному аккаунту MTProto клавиатуру прикрепить не
# даёт, боту Bot API — даёт.


#: Пауза между сообщениями. Telegram разрешает боту около 30 сообщений в
#: секунду разным людям; берём вчетверо медленнее — рассылка не гонка, а
#: словить ограничение на полтысячи человек значит растянуть её сильнее,
#: чем эта пауза за всё время.
CAST_GAP = 0.13

#: Как часто обновлять счётчик в чате владельца. Каждое сообщение —
#: это правка, а правки тоже лимитируются.
CAST_REPORT_EVERY = 25


@router.callback_query(F.data == "a:cast")
async def ask_cast(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(Cast.message)
    people = len(await db.all_recipients())
    await callback.message.edit_text(
        "📣 <b>Рассылка по людям</b>\n\n"
        f"Уйдёт всем, кто когда-либо запускал бота: <b>{people}</b>.\n\n"
        "Пришлите сообщение — текстом или с медиа.\n\n"
        "В <b>тексте</b> работают переменные и оформление:\n"
        + richtext.help_text()
        + "\n\nВ сообщении <b>с медиа</b> переменных нет — фото и гифка "
        "уходят копией, одинаковой для всех.",
        reply_markup=_cancel().as_markup(),
    )
    await callback.answer()


@router.message(Cast.message)
async def cast_got_message(message: Message, state: FSMContext) -> None:
    if message.text:
        # Текст храним разобранным: только так в него можно подставить
        # имя каждого получателя.
        await state.update_data(
            text=message.text,
            entities=richtext.dump(message.entities),
            src_chat=None,
            src_msg=None,
        )
    else:
        # Медиа не пересобираем — копируем, как и приветствие.
        await state.update_data(
            text=None,
            entities=None,
            src_chat=message.chat.id,
            src_msg=message.message_id,
        )
    await state.set_state(Cast.buttons)
    await message.answer(
        "🔘 <b>Кнопки под сообщением</b>\n\n" + _buttons_help() + "\n\n"
        "Без кнопок — пришлите <code>-</code>.",
        reply_markup=_cancel().as_markup(),
    )


@router.message(Cast.buttons, F.text)
async def cast_got_buttons(message: Message, state: FSMContext) -> None:
    raw = (message.text or "").strip()
    buttons, bad = ([], []) if raw == "-" else keyboards.parse_buttons(raw)
    if raw != "-" and not buttons:
        await message.answer(
            "Ни одной кнопки не разобрал. Формат: <code>Текст | ссылка</code>, "
            "по кнопке в строке. Без кнопок — <code>-</code>.",
            reply_markup=_cancel().as_markup(),
        )
        return

    await state.update_data(buttons=buttons)
    data = await state.get_data()
    people = len(await db.all_recipients())

    note = "👀 <b>Вот что уйдёт</b> — проверьте и подтвердите.\n"
    if bad:
        note += "\n⚠️ Не понял строки:\n" + "\n".join(f"• {b}" for b in bad) + "\n"
    note += (
        f"\nПолучателей: <b>{people}</b>, "
        f"займёт примерно <b>{_eta(people)}</b>."
    )

    # Показываем не описание, а само сообщение — ровно то, что увидят
    # люди, с подставленными переменными для самого владельца.
    await _cast_one(message.bot, message.from_user.id, data,
                    keyboards.cast_markup(data.get("buttons")),
                    richtext.person({"user_id": message.from_user.id,
                                     "username": message.from_user.username,
                                     "name": message.from_user.first_name}))

    builder = InlineKeyboardBuilder()
    builder.button(text="📣 Разослать", callback_data="a:cast:go")
    builder.button(text="← Отмена", callback_data="a:home")
    builder.adjust(1, 1)
    await message.answer(note, reply_markup=builder.as_markup())


def _eta(people: int) -> str:
    seconds = int(people * CAST_GAP) + 1
    if seconds < 60:
        return f"{seconds} сек"
    return f"{seconds // 60} мин"


async def _cast_one(bot, user_id: int, data: dict, markup, person=None) -> None:
    """Одно сообщение рассылки одному человеку."""
    if data.get("text"):
        values = await richtext.values_for(person, db)
        text, entities = richtext.apply(
            data["text"], richtext.load(data.get("entities")), values
        )
        await richtext.send(bot, user_id, text, entities, markup)
        return
    await bot.copy_message(
        chat_id=user_id,
        from_chat_id=int(data["src_chat"]),
        message_id=int(data["src_msg"]),
        reply_markup=markup,
    )


@router.callback_query(F.data == "a:cast:go")
async def cast_go(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    if not data.get("text") and not data.get("src_msg"):
        await callback.answer("Нечего рассылать — начните заново", show_alert=True)
        return
    await callback.answer("Начал")
    # Отдельной задачей: рассылка на тысячу человек живёт минуты, а
    # хендлер, который столько не отвечает, Telegram переспросит.
    asyncio.create_task(_run_cast(callback.bot, callback.from_user.id, data))


async def _run_cast(bot, owner_id: int, data: dict) -> None:
    """Разослать всем и рассказать владельцу, чем кончилось.

    Заблокировавших бота считаем отдельно от настоящих ошибок: первое —
    обычное дело и ни о чём не говорит, второе стоит посмотреть.
    """
    from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

    people = await db.all_recipients()
    markup = keyboards.cast_markup(data.get("buttons"))
    started = time.time()
    sent = blocked = failed = 0
    last_error = ""

    status = await bot.send_message(owner_id, f"📣 Рассылаю: 0 из {len(people)}…")

    for number, row in enumerate(people, 1):
        user_id = int(row["user_id"])
        for attempt in (1, 2):
            try:
                await _cast_one(bot, user_id, data, markup, richtext.person(row))
                sent += 1
                break
            except TelegramRetryAfter as error:
                # Единственная ошибка, которую есть смысл переждать:
                # Telegram прямо говорит, сколько именно.
                if attempt == 2:
                    failed += 1
                    break
                await asyncio.sleep(error.retry_after + 1)
            except TelegramForbiddenError:
                blocked += 1
                break
            except Exception as error:  # noqa: BLE001 — рассылку это не валит
                failed += 1
                last_error = str(error)
                log.warning("рассылка: %s не получил — %s", user_id, error)
                break

        await asyncio.sleep(CAST_GAP)
        if number % CAST_REPORT_EVERY == 0:
            try:
                await status.edit_text(
                    f"📣 Рассылаю: {number} из {len(people)}…\n"
                    f"Дошло: {sent}, заблокировали бота: {blocked}"
                )
            except Exception:  # правка не прошла — не повод рвать рассылку
                pass

    took = int(time.time() - started)
    report = (
        "✅ <b>Рассылка закончена</b>\n\n"
        f"Дошло: <b>{sent}</b>\n"
        f"Заблокировали бота: <b>{blocked}</b>\n"
        f"Не дошло: <b>{failed}</b>\n"
        f"Заняло: {took // 60} мин {took % 60} сек"
    )
    if last_error:
        report += f"\n\nПоследняя ошибка: <code>{last_error[:200]}</code>"
    log.info("рассылка: %s дошло, %s заблокировали, %s ошибок",
             sent, blocked, failed)
    try:
        await status.edit_text(report, reply_markup=panel().as_markup())
    except Exception:
        await bot.send_message(owner_id, report, reply_markup=panel().as_markup())


# --- подсказка по премиум-эмодзи --------------------------------------


@router.message(F.text == "/emoji")
async def emoji_hint(message: Message) -> None:
    await message.answer(
        "🧩 <b>Как узнать id премиум-эмодзи</b>\n\n"
        "Пришлите мне сообщение, где есть нужный премиум-эмодзи, — я "
        "отвечу его id. Этот id и ставится третьим полем в строке кнопки."
    )


@router.message(F.entities.func(lambda e: richtext.has_custom_emoji(e)))
async def emoji_ids(message: Message) -> None:
    """Ответить id премиум-эмодзи, если владелец прислал их вне настройки.

    Стоит последним в модуле: в состояниях настройки сообщение заберёт
    свой хендлер, а сюда попадут только письма «просто так». Без этого
    id премиум-эмодзи владельцу пришлось бы искать сторонним ботом.
    """
    ids = []
    for entity in message.entities or []:
        if entity.type == "custom_emoji" and entity.custom_emoji_id not in ids:
            ids.append(entity.custom_emoji_id)
    if not ids:
        return
    await message.answer(
        "🧩 id этих премиум-эмодзи:\n"
        + "\n".join(f"<code>{value}</code>" for value in ids)
        + "\n\nСтавится третьим полем в строке кнопки."
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
