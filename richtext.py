"""Подстановка переменных в текст, сохраняя оформление.

Задача, из-за которой этот модуль вообще есть: в приветствии нужны и
переменные (`{name}`), и премиум-эмодзи с жирным и цитатами — и всё это
не только в тексте, но и в подписи к фото или гифке. Разметку
Telegram передаёт не тегами, а **сущностями** — списком «с такого-то
символа, такой-то длины, такой-то тип». Стоит подставить в текст имя, и
все сущности правее сдвига начинают указывать не туда: жирным окажется
кусок соседнего слова, а премиум-эмодзи — половина буквы.

Поэтому подстановка идёт вместе со сдвигом сущностей.

Второе, на чём здесь легко ошибиться: **смещения считаются в UTF-16**,
а не в символах Python. Для латиницы разницы нет, но эмодзи занимает
два кода UTF-16 и один символ Python — и текст с эмодзи слева от
подстановки уезжает на ровно столько же позиций. Отсюда `_u16` вместо
обычного `len`.
"""

from __future__ import annotations

import re

#: Что можно подставлять. Ключ — имя в фигурных скобках.
VARIABLES = {
    "name": "имя человека",
    "username": "ник со «собакой» или пусто",
    "id": "числовой id",
    "balance": "баланс монет",
    "days": "дней подписки осталось",
    "tariff": "состояние подписки словами",
}


def _u16(text: str) -> int:
    """Длина в кодах UTF-16 — именно в них Telegram считает сущности."""
    return len(text.encode("utf-16-le")) // 2


def apply(text: str, entities, values: dict) -> tuple[str, list]:
    """Подставить переменные и сдвинуть сущности под новый текст.

    `entities` — список объектов или словарей с offset/length/type.
    Возвращает новый текст и новый список сущностей словарями.
    """
    if not text:
        return text, list(entities or [])

    shifted = []
    for entity in entities or []:
        item = entity if isinstance(entity, dict) else entity.model_dump(
            exclude_none=True
        )
        shifted.append(dict(item))

    pattern = re.compile(
        r"\{(" + "|".join(re.escape(key) for key in VARIABLES) + r")\}"
    )

    out, last, shift = [], 0, 0
    for match in pattern.finditer(text):
        out.append(text[last:match.start()])
        replacement = str(values.get(match.group(1), ""))
        out.append(replacement)

        # Позиция плейсхолдера в тех же координатах, в которых сейчас
        # лежат сущности: исходная плюс всё, что сдвинули до неё.
        at = _u16(text[: match.start()]) + shift
        was, now = _u16(match.group(0)), _u16(replacement)
        delta = now - was

        for entity in shifted:
            start = entity.get("offset", 0)
            length = entity.get("length", 0)
            if start >= at + was:
                # Сущность целиком правее — едет вместе с текстом.
                entity["offset"] = start + delta
            elif start <= at and start + length >= at + was:
                # Плейсхолдер внутри сущности — она растягивается.
                entity["length"] = length + delta

        shift += delta
        last = match.end()

    out.append(text[last:])
    result = "".join(out)

    # Сущности нулевой длины Telegram не примет, а после подстановки
    # пустой строкой такие появляются.
    return result, [e for e in shifted if e.get("length", 0) > 0]


async def values_for(user, db) -> dict:
    """Значения переменных для конкретного человека."""
    user_id = getattr(user, "id", 0) or 0
    subscription = await db.subscription(user_id)
    if subscription.kind == "paid":
        tariff = "подписка активна"
    elif subscription.kind == "trial":
        tariff = "пробный период"
    else:
        tariff = "бесплатный период закончился"
    return {
        "name": (getattr(user, "first_name", None) or "друг"),
        "username": (f"@{user.username}" if getattr(user, "username", None) else ""),
        "id": user_id,
        "balance": await db.coins_of(user_id),
        "days": subscription.days_left,
        "tariff": tariff,
    }


def help_text() -> str:
    lines = "\n".join(
        f"• <code>{{{key}}}</code> — {what}" for key, what in VARIABLES.items()
    )
    return "Доступные переменные:\n" + lines


def dump(entities) -> str:
    """Сущности сообщения в JSON — в таком виде они лежат в базе."""
    import json

    items = []
    for entity in entities or []:
        item = entity if isinstance(entity, dict) else entity.model_dump(
            exclude_none=True
        )
        # У упоминания внутри сущности лежит целый объект пользователя;
        # в приветствии он бесполезен, а в базе занимает место.
        item.pop("user", None)
        items.append(item)
    return json.dumps(items, ensure_ascii=False)


def load(raw: str) -> list[dict]:
    import json

    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return items if isinstance(items, list) else []


def to_entities(items) -> list:
    """Словари обратно в объекты aiogram.

    Сущность неизвестного типа — не повод уронить приветствие: её просто
    не будет, остальное оформление доедет.
    """
    from aiogram.types import MessageEntity

    out = []
    for item in items or []:
        try:
            out.append(MessageEntity.model_validate(item))
        except Exception:  # noqa: BLE001 — тип из будущей версии Telegram
            continue
    return out


def has_custom_emoji(entities) -> bool:
    for entity in entities or []:
        kind = entity.get("type") if isinstance(entity, dict) else getattr(
            entity, "type", ""
        )
        if kind == "custom_emoji":
            return True
    return False


def drop_custom_emoji(entities) -> list:
    """Убрать премиум-эмодзи, оставив остальное оформление.

    Нужно как запасной путь: премиум-эмодзи в *тексте* бот вправе слать
    не всегда — Telegram разрешает это боту с купленным на Fragment
    именем. Если Telegram откажет, лучше отправить то же сообщение без
    значков, чем не отправить ничего.
    """
    out = []
    for entity in entities or []:
        kind = entity.get("type") if isinstance(entity, dict) else getattr(
            entity, "type", ""
        )
        if kind != "custom_emoji":
            out.append(entity)
    return out


async def send_copy(bot, chat_id: int, from_chat_id: int, message_id: int,
                    caption: str | None, entities, values: dict, markup=None):
    """Скопировать медиа, подменив подпись подставленной.

    Так медиа и переменные уживаются в одном сообщении. Само медиа
    по-прежнему копия — фото, гифка и видео переносятся целиком, ничего
    не скачивается. А подпись `copyMessage` разрешает **заменить**, и в
    заменённую уже можно подставить имя и баланс.

    Иначе пришлось бы выбирать: либо картинка, либо обращение по имени.
    """
    from aiogram.exceptions import TelegramBadRequest

    text, shifted = (caption, list(entities or []))
    if caption:
        text, shifted = apply(caption, entities, values)

    async def attempt(items):
        return await bot.copy_message(
            chat_id=chat_id,
            from_chat_id=int(from_chat_id),
            message_id=int(message_id),
            # Подпись без переменных передаём как есть: подменять её той
            # же самой незачем, а у стикера с кружком её и не бывает.
            caption=text if caption else None,
            caption_entities=to_entities(items) if caption else None,
            parse_mode=None,
            reply_markup=markup,
        )

    try:
        return await attempt(shifted)
    except TelegramBadRequest:
        if not has_custom_emoji(shifted):
            raise
        return await attempt(drop_custom_emoji(shifted))


async def send(bot, chat_id: int, text: str, entities, markup=None):
    """Отправить оформленный текст, пережив отказ из-за премиум-эмодзи.

    Разметку передаём сущностями, а не HTML, и поэтому глушим общий
    parse_mode: иначе Telegram разберёт текст ещё и как разметку, а
    угловая скобка в чьём-нибудь имени превратится в ошибку.
    """
    from aiogram.exceptions import TelegramBadRequest

    try:
        return await bot.send_message(
            chat_id,
            text,
            entities=to_entities(entities),
            parse_mode=None,
            reply_markup=markup,
        )
    except TelegramBadRequest:
        if not has_custom_emoji(entities):
            raise
        return await bot.send_message(
            chat_id,
            text,
            entities=to_entities(drop_custom_emoji(entities)),
            parse_mode=None,
            reply_markup=markup,
        )


def person(row: dict):
    """Строка из базы в том же виде, в каком приходит `message.from_user`.

    Нужно, чтобы подстановка переменных не знала, откуда взялся человек:
    из апдейта Telegram или из выборки получателей рассылки.
    """
    from types import SimpleNamespace

    full = (row.get("name") or "").strip()
    return SimpleNamespace(
        id=int(row.get("user_id") or 0),
        first_name=full.split(" ")[0] if full else None,
        last_name=None,
        username=row.get("username"),
    )
