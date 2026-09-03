"""Чаты и папки подключённого аккаунта: сканирование и кэш.

Список диалогов читается у Telegram и кладётся в базу. Читается по
кнопке, а не при каждом открытии приложения: у активного аккаунта это
сотни диалогов и заметный запрос, а Telegram не любит, когда его дёргают
часто.

Вместе с каждым чатом сохраняется **access_hash**, и это не мелочь.
Строка сессии несёт только ключ авторизации — справочник знакомых чатов
в неё не входит. Клиент, поднятый из сохранённой сессии, о чате с
известным id ничего не знает и отправить туда сообщение не может, пока
не вычитает диалоги заново. Хранение хэша снимает это: InputPeer
собирается прямо из базы, без единого лишнего запроса перед отправкой.

Папки Telegram (они же dialog filters) разворачиваются в список чатов
здесь же. Папка бывает двух видов: собранная руками (в ней перечислены
конкретные чаты) и заданная правилом («все группы»). Разворачиваются оба
вида — правило применяется к уже вычитанным диалогам.
"""

from __future__ import annotations

import logging
import time

import accounts
import db

log = logging.getLogger("rassylka.chats")

#: Сколько диалогов вычитываем максимум. У людей с тысячами чатов полный
#: обход занимает минуты и злит Telegram, а рассылают почти всегда по
#: недавним.
SCAN_LIMIT = 500


def _kind(entity) -> str:
    """user | chat | channel — от этого зависит вид InputPeer при отправке."""
    name = type(entity).__name__
    if name == "User":
        return "user"
    if name == "Chat" or name == "ChatForbidden":
        return "chat"
    return "channel"


def _title(entity) -> str:
    parts = [
        getattr(entity, "title", None),
        " ".join(
            part
            for part in (
                getattr(entity, "first_name", None),
                getattr(entity, "last_name", None),
            )
            if part
        ),
        getattr(entity, "username", None),
    ]
    for part in parts:
        if part and part.strip():
            return part.strip()
    return "Без названия"


async def _read_dialogs(client) -> list[dict]:
    from telethon import utils

    rows: list[dict] = []
    async for dialog in client.iter_dialogs(limit=SCAN_LIMIT):
        entity = dialog.entity
        if entity is None:
            continue
        # Боты в рассылке бесполезны: писать роботам нечего, а в списке
        # они занимают место и путают.
        if getattr(entity, "bot", False):
            continue
        rows.append(
            {
                "chat_id": utils.get_peer_id(entity),
                "raw_id": entity.id,
                "access_hash": getattr(entity, "access_hash", None),
                "kind": _kind(entity),
                "title": _title(entity),
                "username": getattr(entity, "username", None),
                # Канал или супергруппа: у Telegram это один тип, а
                # различает их флаг. Нужен и папкам вида «все группы»,
                # и списку чатов в приложении.
                "broadcast": bool(getattr(entity, "broadcast", False)),
            }
        )
    return rows


def _folder_title(raw) -> str:
    """Название папки.

    В свежих слоях это не строка, а TextWithEntities — там же живут
    эмодзи из названия. Со старым клиентом придёт обычная строка,
    поэтому берём аккуратно.
    """
    title = getattr(raw, "title", None)
    return str(getattr(title, "text", title) or "").strip() or "Папка"


async def _read_folders(client, known: list[dict]) -> list[dict]:
    from telethon import utils
    from telethon.tl import functions

    try:
        answer = await client(functions.messages.GetDialogFiltersRequest())
    except Exception as error:
        # Папок может не быть вовсе, а на старых слоях метода нет.
        # Это не повод заваливать всё сканирование.
        log.info("папки не прочитались: %s", error)
        return []

    raw_filters = getattr(answer, "filters", answer) or []
    by_kind: dict[str, list[int]] = {"user": [], "chat": [], "channel": []}
    for row in known:
        by_kind[row["kind"]].append(row["chat_id"])

    out: list[dict] = []
    for raw in raw_filters:
        # DialogFilterDefault — это «все чаты», псевдопапка без id.
        folder_id = getattr(raw, "id", None)
        if folder_id is None:
            continue

        chat_ids: list[int] = []
        for peer in list(getattr(raw, "pinned_peers", []) or []) + list(
            getattr(raw, "include_peers", []) or []
        ):
            try:
                chat_ids.append(utils.get_peer_id(peer))
            except Exception:
                continue

        # Папка, заданная правилом: конкретные чаты в ней не перечислены,
        # зато выставлены флаги вида «все группы». Разворачиваем правило
        # по уже вычитанным диалогам — иначе такая папка выглядела бы
        # пустой, хотя в клиенте в ней сотня чатов.
        if getattr(raw, "groups", False):
            chat_ids += by_kind["chat"] + [
                row["chat_id"] for row in known
                if row["kind"] == "channel" and not row.get("broadcast")
            ]
        if getattr(raw, "broadcasts", False):
            chat_ids += [
                row["chat_id"] for row in known
                if row["kind"] == "channel" and row.get("broadcast")
            ]
        if getattr(raw, "contacts", False) or getattr(raw, "non_contacts", False):
            chat_ids += by_kind["user"]

        excluded = set()
        for peer in getattr(raw, "exclude_peers", []) or []:
            try:
                excluded.add(utils.get_peer_id(peer))
            except Exception:
                continue

        # Порядок сохраняем, повторы убираем: чат может быть и
        # закреплённым, и попавшим под правило одновременно, а писать в
        # него дважды за круг не нужно.
        seen: set[int] = set()
        ordered = [
            chat_id
            for chat_id in chat_ids
            if chat_id not in excluded
            and not (chat_id in seen or seen.add(chat_id))
        ]
        out.append(
            {
                "folder_id": int(folder_id),
                "title": _folder_title(raw),
                "chat_ids": ordered,
            }
        )
    return out


#: Сколько сообщений из «Избранного» показываем как материалы.
SAVED_LIMIT = 50


def _material_kind(message) -> str:
    """Что это за сообщение — для значка в списке."""
    if getattr(message, "sticker", None):
        return "sticker"
    if getattr(message, "gif", None):
        return "gif"
    if getattr(message, "video", None):
        return "video"
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return "audio"
    if getattr(message, "photo", None):
        return "photo"
    if getattr(message, "document", None):
        return "document"
    return "text"


def _preview(message, kind: str) -> str:
    """Короткая подпись для списка материалов."""
    text = (getattr(message, "message", None) or "").strip()
    if text:
        return text[:120]
    labels = {
        "sticker": "Стикер",
        "gif": "Гифка",
        "video": "Видео",
        "audio": "Аудио",
        "photo": "Фото",
        "document": "Файл",
    }
    return labels.get(kind, "Сообщение")


async def _read_saved(client) -> list[dict]:
    """Последние сообщения из «Избранного» аккаунта."""
    rows: list[dict] = []
    async for message in client.iter_messages("me", limit=SAVED_LIMIT):
        # Служебные сообщения (кто-то вошёл, чат создан) материалом быть
        # не могут: у них нет ни текста, ни вложения.
        if getattr(message, "action", None) is not None:
            continue
        kind = _material_kind(message)
        if kind == "text" and not (message.message or "").strip():
            continue
        rows.append(
            {
                "msg_id": message.id,
                "kind": kind,
                "preview": _preview(message, kind),
                "has_media": getattr(message, "media", None) is not None,
            }
        )
    return rows


async def scan(user_id: int, account_id: int) -> dict:
    """Перечитать диалоги и папки аккаунта. Возвращает, что нашлось."""
    account = await db.account(user_id, account_id)
    if account is None:
        raise accounts.LoginError("Такого аккаунта нет.")

    client = None
    try:
        client = await accounts.client_for(account)
        if not await client.is_user_authorized():
            await db.mark_account(account.id, "dead", "сессия отозвана в Telegram")
            raise accounts.LoginError(
                "Аккаунт больше не в сети — подключите его заново.", restart=True
            )
        found = await _read_dialogs(client)
        folders = await _read_folders(client, found)
        # «Избранное» читается тем же заходом: отдельная кнопка ради
        # него — лишний поход в Telegram и лишний шаг для человека.
        saved = await _read_saved(client)
    except accounts.LoginError:
        raise
    except Exception as error:
        from telethon import errors

        if isinstance(error, errors.FloodWaitError):
            await db.pause_account(
                account.id,
                int(time.time()) + error.seconds,
                "FloodWait при сканировании",
            )
            raise accounts.LoginError(
                f"Telegram просит подождать {error.seconds // 60 or 1} мин "
                "перед следующим обращением."
            )
        log.exception("сканирование аккаунта %s сорвалось", account_id)
        raise accounts.LoginError(f"Не удалось прочитать чаты: {type(error).__name__}")
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    await db.save_chats(account.id, found)
    await db.save_folders(account.id, folders)
    await db.save_materials(account.id, saved)
    log.info(
        "аккаунт %s: чатов %s, папок %s, материалов %s",
        account_id, len(found), len(folders), len(saved),
    )
    return {
        "chats": len(found),
        "folders": len(folders),
        "materials": len(saved),
    }
