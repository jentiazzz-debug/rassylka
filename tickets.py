"""Тикеты поддержки: переписка человека с владельцем через бота.

Зачем отдельно от лички. Написать в поддержку можно и просто сообщением,
но тогда обращения перемешиваются с уведомлениями о рассылках и
теряются, а у владельца нет списка «на что ещё не ответил» — по чату его
не составить. Тикет же виден с обеих сторон: человек в приложении, где и
завёл его, владелец — в `/admin`.

**Медиа не хранится у нас.** Приложение отправляет файл на сервер, бот
пересылает его владельцу и запоминает `file_id`. По этому идентификатору
Telegram потом отдаёт файл сколько угодно раз и кому угодно из тех, с
кем бот переписывается, а на нашем диске не остаётся ничего. Свою
файлопомойку заводить ради вложений в тикеты незачем.

**Статус ведёт переписка, а не кнопки.** `open` — ждёт владельца,
`answered` — владелец ответил, `closed` — закрыт. Он полностью
определяется тем, кто написал последним, и потому меняется там же, где
добавляется сообщение: отдельный вызов однажды забыли бы, и список
«что разобрать» разошёлся бы с действительностью.
"""

from __future__ import annotations

import html
import logging

import config
import db

log = logging.getLogger("rassylka.tickets")

#: Какие вложения принимаем и чем их слать обратно. Ключ — то, что
#: приходит из приложения, значение — метод бота.
KINDS = {
    "photo": "send_photo",
    "video": "send_video",
    "animation": "send_animation",
    "voice": "send_voice",
    "audio": "send_audio",
    "document": "send_document",
}

MAX_SUBJECT = 120
MAX_BODY = 2000


def _who(user: dict) -> str:
    """Как назвать человека в уведомлении владельцу."""
    name = html.escape(str(user.get("name") or "").strip())
    nick = str(user.get("username") or "").strip()
    parts = [name] if name else []
    if nick:
        parts.append(f"@{html.escape(nick)}")
    parts.append(f"<code>{user.get('user_id')}</code>")
    return " · ".join(parts)


async def notify_admins(bot, text: str, ticket_id: int) -> None:
    """Сказать владельцам про новое обращение.

    Уведомление уходит всем админам, а не первому попавшемуся: если их
    двое, «кто-нибудь да увидит» — не план.
    """
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    builder = InlineKeyboardBuilder()
    builder.button(text="✉️ Открыть тикет", callback_data=f"a:t:{ticket_id}")

    for admin_id in config.ADMIN_IDS:
        try:
            await bot.send_message(admin_id, text, reply_markup=builder.as_markup())
        except Exception as error:  # владелец мог не начинать чат с ботом
            log.debug("уведомление о тикете не ушло %s: %s", admin_id, error)


async def deliver(bot, chat_id: int, message: dict, header: str = "") -> None:
    """Показать сообщение тикета в чате: текст и вложение, если оно есть.

    Вложение уходит тем же типом, каким пришло: гифка гифкой, голосовое
    голосовым. Слать всё документом было бы проще, но голосовое,
    приехавшее файлом, слушать невозможно.
    """
    text = str(message.get("text") or "")
    body = (header + "\n\n" if header else "") + html.escape(text)

    kind = message.get("file_kind")
    file_id = message.get("file_id")
    if not kind or not file_id or kind not in KINDS:
        await bot.send_message(chat_id, body or header or "—")
        return

    send = getattr(bot, KINDS[kind])
    try:
        await send(chat_id, file_id, caption=body[:1024] or None)
    except Exception as error:
        # Файл мог протухнуть или тип не совпасть с методом. Терять из-за
        # этого текст обращения нельзя — он важнее вложения.
        log.warning("вложение тикета не ушло (%s): %s", kind, error)
        await bot.send_message(chat_id, body or header or "—")


async def open_ticket(bot, user: dict, subject: str, body: str,
                      file_kind: str | None = None,
                      file_id: str | None = None,
                      file_name: str | None = None) -> int:
    """Завести тикет и показать его владельцу."""
    ticket_id = await db.create_ticket(int(user["user_id"]), subject)
    await db.add_ticket_message(
        ticket_id, "user", body, file_kind, file_id, file_name
    )

    header = (
        f"🎫 <b>Тикет #{ticket_id}</b>\n"
        f"{_who(user)}\n\n"
        f"<b>{html.escape(subject)}</b>"
    )
    for admin_id in config.ADMIN_IDS:
        try:
            await deliver(
                bot, admin_id,
                {"text": body, "file_kind": file_kind, "file_id": file_id},
                header,
            )
        except Exception as error:
            log.debug("тикет не показался владельцу %s: %s", admin_id, error)
    await notify_admins(bot, f"Ответить на тикет #{ticket_id}?", ticket_id)
    log.info("тикет #%s от %s: %s", ticket_id, user.get("user_id"), subject)
    return ticket_id


async def user_reply(bot, user: dict, ticket_id: int, body: str,
                     file_kind: str | None = None,
                     file_id: str | None = None,
                     file_name: str | None = None) -> None:
    """Ответ человека в свой тикет."""
    await db.add_ticket_message(
        ticket_id, "user", body, file_kind, file_id, file_name
    )
    header = f"🎫 <b>Тикет #{ticket_id}</b> — новое сообщение\n{_who(user)}"
    for admin_id in config.ADMIN_IDS:
        try:
            await deliver(
                bot, admin_id,
                {"text": body, "file_kind": file_kind, "file_id": file_id},
                header,
            )
        except Exception as error:
            log.debug("ответ в тикет не показался %s: %s", admin_id, error)


async def admin_reply(bot, ticket_id: int, body: str,
                      file_kind: str | None = None,
                      file_id: str | None = None) -> bool:
    """Ответ владельца. False — тикета нет."""
    found = await db.ticket(ticket_id)
    if found is None:
        return False

    await db.add_ticket_message(ticket_id, "admin", body, file_kind, file_id)
    header = f"💬 <b>Ответ поддержки — тикет #{ticket_id}</b>"
    try:
        await deliver(
            bot, int(found["user_id"]),
            {"text": body, "file_kind": file_kind, "file_id": file_id},
            header,
        )
    except Exception as error:
        # Человек мог заблокировать бота. Ответ всё равно записан и
        # виден в приложении — терять его из-за этого нельзя.
        log.info("ответ на тикет #%s не доставлен: %s", ticket_id, error)
    return True


def file_from(message) -> tuple[str | None, str | None, str | None]:
    """Что за вложение в сообщении бота: (тип, file_id, имя файла).

    Порядок проверок не случаен: у гифки есть и animation, и document, у
    голосового — и voice, и document. Общий тип должен победить, иначе
    гифка уедет обратно файлом.
    """
    if getattr(message, "photo", None):
        return "photo", message.photo[-1].file_id, None
    for kind in ("animation", "video", "voice", "audio", "document"):
        item = getattr(message, kind, None)
        if item is not None:
            return kind, item.file_id, getattr(item, "file_name", None)
    return None, None, None
