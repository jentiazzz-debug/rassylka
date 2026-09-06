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

import json

import config
import db
import keyboards
import richtext
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


def ref_payload(user_id: int) -> str:
    """Метка приглашения в ссылке. Буква впереди — чтобы отличить её от
    любых других payload, которые появятся позже."""
    return f"r{user_id}"


def ref_from(payload: str) -> int:
    """Кто пригласил. 0 — payload не про приглашение."""
    payload = (payload or "").strip()
    if payload.startswith("r") and payload[1:].isdigit():
        return int(payload[1:])
    return 0


async def invite_link(bot, user_id: int) -> str:
    me = await bot.me()
    return f"https://t.me/{me.username}?start={ref_payload(user_id)}"


async def _handle_referral(message: Message) -> None:
    """Записать пригласившего и начислить ему монеты.

    Делается после того, как приглашённый уже заведён в базе: ref_by
    ставится один раз и только тому, у кого его ещё нет, — иначе
    приглашённого переприсвоила бы любая следующая ссылка.
    """
    user = message.from_user
    parts = (message.text or "").split(maxsplit=1)
    inviter = ref_from(parts[1]) if len(parts) > 1 else 0
    if not inviter or user is None:
        return
    if not await db.set_referrer(user.id, inviter):
        return
    if not config.REF_COINS:
        return
    balance = await db.add_coins(
        inviter, config.REF_COINS, "приглашённый пришёл по ссылке"
    )
    log.info("приглашение: %s привёл %s", inviter, user.id)
    try:
        await message.bot.send_message(
            inviter, texts.referral_joined(config.REF_COINS, balance)
        )
    except Exception as error:  # пригласивший мог закрыть личку
        log.debug("уведомление о приглашении не ушло: %s", error)


@router.message(Command("invite"))
async def invite_command(message: Message) -> None:
    await _remember(message)
    user = message.from_user
    if user is None:
        return
    await message.answer(
        texts.invite(
            await invite_link(message.bot, user.id),
            await db.referral_stats(user.id),
        ),
        reply_markup=keyboards.main_menu(),
    )


async def custom_buttons() -> list[dict] | None:
    """Кнопки, настроенные владельцем. None — обычные."""
    raw = await db.setting("menu_buttons")
    if not raw:
        return None
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("кнопки меню в базе испорчены — показываю обычные")
        return None
    return items if isinstance(items, list) and items else None


async def send_start(bot, chat_id: int, user, preview: bool = False) -> None:
    """Приветствие: своё, настроенное владельцем, либо обычное.

    Переменные (`{name}`, `{balance}`) работают в любом своём
    приветствии — и в тексте, и в подписи к фото. А вот путь до человека
    у текста и медиа разный.

    **Текст** лежит в базе разобранным — самим текстом и сущностями
    разметки — и собирается заново на каждого читателя: только так в
    него можно подставить имя.

    **Медиа** копируется целиком через `copy_message`: так доезжают
    фото, гифка, кружок, стикер, премиум-эмодзи. Разбирать и собирать
    заново — значит потерять половину: у премиум-эмодзи нужен доступ к
    их документам, у медиа свои file_id. Подпись при этом подставляется
    всё равно, потому что копирование разрешает её **заменить**.
    """
    markup = keyboards.main_menu(await custom_buttons())

    raw = await db.setting("menu_text")
    if raw:
        values = await richtext.values_for(user, db)
        text, entities = richtext.apply(raw, richtext.load(
            await db.setting("menu_entities")), values)
        await richtext.send(bot, chat_id, text, entities, markup)
        return

    chat = await db.setting("menu_chat_id")
    msg_id = await db.setting("menu_msg_id")
    if chat and msg_id:
        caption = await db.setting("menu_caption")
        try:
            await richtext.send_copy(
                bot, chat_id, int(chat), int(msg_id),
                caption or None,
                richtext.load(await db.setting("menu_caption_entities")),
                await richtext.values_for(user, db) if caption else {},
                markup,
            )
            return
        except Exception as error:
            # Исходное сообщение удалили или бот потерял к нему доступ.
            # Молчать нельзя: владелец будет думать, что оформление
            # работает, а люди видят обычный текст.
            log.warning("своё приветствие не скопировалось: %s", error)
            for key in ("menu_chat_id", "menu_msg_id",
                        "menu_caption", "menu_caption_entities"):
                await db.set_setting(key, None)
            for admin_id in config.ADMIN_IDS:
                try:
                    await bot.send_message(admin_id, texts.GREETING_LOST)
                except Exception:
                    pass

    subscription = await db.subscription(getattr(user, "id", 0) or 0)
    await bot.send_message(
        chat_id,
        texts.start(getattr(user, "first_name", None), subscription),
        reply_markup=markup,
    )


@router.message(CommandStart())
async def start(message: Message) -> None:
    await _remember(message)
    await _handle_referral(message)
    await send_start(message.bot, message.chat.id, message.from_user)
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


@router.message(Command("terms"))
async def terms_command(message: Message) -> None:
    """Документы и тарифы — ссылками, а не текстом.

    Банк и платёжная система смотрят именно на страницы: им нужен
    открывающийся в браузере адрес, а не сообщение в чате.
    """
    await _remember(message)
    if not config.WEBAPP_URL:
        await message.answer(texts.NO_WEBAPP)
        return
    await message.answer(
        texts.documents(config.WEBAPP_URL), reply_markup=keyboards.main_menu()
    )


@router.message(Command("tariffs"))
async def tariffs_command(message: Message) -> None:
    await _remember(message)
    await message.answer(
        texts.tariffs(),
        reply_markup=keyboards.main_menu(),
        disable_web_page_preview=True,
    )


@router.message(Command("stats"))
async def stats_command(message: Message) -> None:
    user = message.from_user
    if user is None or user.id not in config.ADMIN_IDS:
        return
    await message.answer(texts.stats(await db.stats()))


@router.callback_query(F.data == "m:help")
async def help_button(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        texts.HELP, reply_markup=keyboards.docs_menu()
    )
    await callback.answer()


@router.callback_query(F.data == "m:docs")
async def docs_button(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        texts.documents(config.WEBAPP_URL) if config.WEBAPP_URL
        else texts.NO_WEBAPP,
        reply_markup=keyboards.docs_menu(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "m:tariffs")
async def tariffs_button(callback: CallbackQuery) -> None:
    await callback.message.edit_text(
        texts.tariffs(),
        reply_markup=keyboards.docs_menu(),
        disable_web_page_preview=True,
    )
    await callback.answer()


@router.callback_query(F.data == "m:home")
async def home_button(callback: CallbackQuery) -> None:
    user = callback.from_user
    subscription = await db.subscription(user.id)
    # Кнопка «назад» правит уже отправленное сообщение, поэтому здесь
    # всегда текст: превратить его в фото или гифку правкой нельзя.
    # Своё оформление увидят при следующем /start.
    await callback.message.edit_text(
        texts.start(user.first_name, subscription),
        reply_markup=keyboards.main_menu(await custom_buttons()),
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
    await send_start(message.bot, message.chat.id, message.from_user)
