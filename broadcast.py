"""Движок рассылки: круг по чатам с паузой между сообщениями.

Как это работает. У рассылки есть список чатов, текст и интервал.
Движок раз в несколько секунд смотрит, кому пора писать, берёт
**следующий чат по кругу**, отправляет туда сообщение и назначает
следующую отправку через интервал. Дошёл до конца списка — начинает
сначала. То есть интервал — это пауза между сообщениями, а не между
кругами: круг по десяти чатам с интервалом в минуту займёт десять минут.

Почему всё в одной очереди, а не «задача на чат». Telegram считает
сообщения аккаунта, а не рассылки. Параллельная отправка в десять чатов
разом — это ровно тот всплеск, за который выдают ограничения. Здесь в
любой момент времени идёт одна отправка, а между любыми двумя стоит
пауза — общая на аккаунт, даже если рассылок у него несколько.

Три тормоза, которые нельзя обойти из интерфейса:

* `MIN_INTERVAL` — интервал в пять секунд не «быстрая рассылка», а
  заявка на блокировку в первый же час;
* `ACCOUNT_MIN_GAP` — общий разрыв между сообщениями аккаунта;
* `DAILY_LIMIT` — потолок за сутки, считается по журналу отправок.

Подключения к Telegram переиспользуются: подключение занимает секунды, и
делать его на каждое сообщение — это и медленно, и лишние логины со
стороны Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field
from typing import Any

import accounts
import config
import db
import texts

log = logging.getLogger("rassylka.broadcast")


# --- пул подключений --------------------------------------------------


@dataclass
class _Live:
    client: Any
    used_at: float
    #: Одно подключение — одна отправка за раз. Telethon выдержал бы и
    #: параллельные, но нам как раз не нужно, чтобы аккаунт писал в два
    #: чата одновременно.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_clients: dict[int, _Live] = {}


async def _live(account: db.Account) -> _Live:
    live = _clients.get(account.id)
    if live is not None and live.client.is_connected():
        live.used_at = time.monotonic()
        return live
    if live is not None:
        _clients.pop(account.id, None)
    client = await accounts.client_for(account)
    live = _Live(client=client, used_at=time.monotonic())
    _clients[account.id] = live
    return live


async def _close(account_id: int) -> None:
    live = _clients.pop(account_id, None)
    if live is None:
        return
    try:
        await live.client.disconnect()
    except Exception as error:
        log.debug("подключение %s не закрылось: %s", account_id, error)


async def close_idle() -> None:
    """Закрыть подключения, которыми давно не пользовались."""
    now = time.monotonic()
    for account_id, live in list(_clients.items()):
        if now - live.used_at > config.CLIENT_IDLE and not live.lock.locked():
            await _close(account_id)


async def close_all() -> None:
    for account_id in list(_clients):
        await _close(account_id)


# --- разбор ошибок Telegram -------------------------------------------

#: Ошибки, из-за которых страдает один чат, а не аккаунт: круг едет
#: дальше, чат отмечается в журнале. Названия, а не классы: набор
#: исключений у Telethon от версии к версии меняется, и обращение к
#: несуществующему классу уронило бы разбор целиком.
SKIP_REASONS = {
    "ChatWriteForbiddenError": "нельзя писать в этот чат",
    "ChatAdminRequiredError": "нужны права администратора",
    "UserBannedInChannelError": "аккаунт забанен в этом чате",
    "ChannelPrivateError": "чат закрыт или аккаунт из него удалён",
    "ChatRestrictedError": "чат ограничен",
    "UserIsBlockedError": "человек заблокировал аккаунт",
    "UserPrivacyRestrictedError": "настройки приватности не дают написать",
    "InputUserDeactivatedError": "аккаунт получателя удалён",
    "UserDeactivatedError": "аккаунт получателя удалён",
    "PeerIdInvalidError": "чат больше не доступен",
    "ChannelInvalidError": "чат больше не доступен",
    "MsgIdInvalidError": "чат больше не доступен",
    "TopicClosedError": "тема в чате закрыта",
    "ChatSendPlainForbiddenError": "в чате запрещены текстовые сообщения",
}

#: Ошибки, после которых аккаунт больше не работает и нужен новый вход.
DEAD_REASONS = {
    "AuthKeyUnregisteredError": "сессия отозвана",
    "AuthKeyInvalidError": "сессия недействительна",
    "SessionRevokedError": "сессия отозвана",
    "SessionExpiredError": "сессия истекла",
    "UserDeactivatedBanError": "аккаунт заблокирован в Telegram",
    "UnauthorizedError": "аккаунт разлогинен",
}


def classify(error: Exception) -> tuple[str, str, int]:
    """Что случилось и что с этим делать: (действие, причина, секунды)."""
    name = type(error).__name__
    seconds = int(getattr(error, "seconds", 0) or 0)

    if name == "SlowModeWaitError":
        return "slowmode", "в чате включён медленный режим", seconds
    if name == "PeerFloodError":
        return "peerflood", "Telegram ограничил аккаунт за рассылку", 0
    if name.startswith("FloodWait") or (seconds and "Flood" in name):
        return "flood", f"Telegram просит подождать {seconds} с", seconds
    if name in DEAD_REASONS:
        return "dead", DEAD_REASONS[name], 0
    if name in SKIP_REASONS:
        return "skip", SKIP_REASONS[name], 0
    return "unknown", name, 0


# --- отправка ---------------------------------------------------------


def _input_peer(chat: db.Chat):
    """Собрать адресата прямо из базы, без обращения к Telegram.

    Ради этого при сканировании и сохранялся access_hash: клиент,
    поднятый из строки сессии, чужих чатов не помнит, и без хэша пришлось
    бы перед каждым сообщением заново вычитывать все диалоги.
    """
    from telethon.tl.types import InputPeerChannel, InputPeerChat, InputPeerUser

    if chat.kind == "channel":
        return InputPeerChannel(chat.raw_id, chat.access_hash or 0)
    if chat.kind == "chat":
        return InputPeerChat(chat.raw_id)
    return InputPeerUser(chat.raw_id, chat.access_hash or 0)


async def compose(user_id: int, text: str) -> str:
    """Текст сообщения с подписью бесплатного тарифа, если он бесплатный.

    Подпись дописывается в конец, а не в начало: у сообщения с медиа
    смещения оформления считаются от начала подписи, и вставка спереди
    сдвинула бы все жирные куски и премиум-эмодзи на длину подписи.
    """
    subscription = await db.subscription(user_id)
    if subscription.paid or not config.FREE_FOOTER:
        return text
    if not text.strip():
        return config.FREE_FOOTER
    return f"{text}\n\n{config.FREE_FOOTER}"


def _delay(interval: int) -> int:
    """Пауза до следующего сообщения, с разбросом.

    Ровный ритм секунда в секунду — самый заметный признак робота из
    всех, и первое, на что смотрят антиспам-правила.
    """
    base = max(config.MIN_INTERVAL, interval)
    spread = base * max(0.0, config.INTERVAL_JITTER)
    return max(config.MIN_INTERVAL, int(base + random.uniform(-spread, spread)))


class MaterialGone(Exception):
    """Сообщение-материал пропало из «Избранного»."""


async def _deliver(client, campaign: db.Campaign, peer) -> None:
    """Отправить в чат то, что задано рассылкой.

    Материал — это сообщение из «Избранного» аккаунта, и отправляется он
    копированием оттуда. Так переживает всё, что человек в него положил:
    премиум-эмодзи и стикеры, цитаты, жирный с курсивом, фото, гифки,
    видео. Разбирать и пересобирать это вручную бессмысленно — половина
    сущностей всё равно потерялась бы.

    Сообщение перечитывается перед каждой отправкой. Не из
    расточительности: у файлов есть file_reference, он живёт считанные
    часы, и сохранённая ссылка на медиа через сутки перестаёт работать.
    Свежее чтение выдаёт свежую ссылку.
    """
    if campaign.content != "saved" or not campaign.saved_id:
        # parse_mode=None намеренно: текст пишут в обычное поле, и
        # звёздочки с подчёркиваниями в нём должны остаться собой, а не
        # превратиться в разметку или сломать отправку.
        text = await compose(campaign.user_id, campaign.text)
        await client.send_message(peer, text, parse_mode=None)
        return

    source = await client.get_messages("me", ids=campaign.saved_id)
    if source is None:
        raise MaterialGone()

    if getattr(source, "sticker", None) is not None:
        # У стикера подписи не бывает — Telegram её не примет. Значит, и
        # подпись бесплатного тарифа к нему не пристаёт: рассылка одними
        # стикерами уходит без неё.
        await client.send_file(peer, source.media)
        return

    caption = await compose(campaign.user_id, source.message or "")
    if source.media is not None:
        # Медиа переотправляется тем же объектом, без скачивания и
        # повторной загрузки: файл уже лежит у Telegram, и аккаунт имеет
        # к нему доступ — он же его туда и положил.
        await client.send_file(
            peer,
            source.media,
            caption=caption or None,
            formatting_entities=source.entities or None,
            parse_mode=None,
        )
        return

    await client.send_message(
        peer,
        caption,
        formatting_entities=source.entities or None,
        parse_mode=None,
    )


async def _targets(campaign: db.Campaign) -> list[int]:
    """Чаты рассылки по порядку.

    Для папки список берётся заново при каждой отправке: человек мог
    добавить в неё чат в Telegram, и рассылка должна это подхватить
    после ближайшего обновления списка, а не при пересоздании.
    """
    if campaign.source == "folder" and campaign.folder_id is not None:
        found = await db.folder(campaign.account_id, campaign.folder_id)
        return found.chat_ids if found else []
    return await db.campaign_targets(campaign.id)


async def _advance(campaign: db.Campaign, total: int, ok: bool) -> None:
    cursor = (campaign.cursor + 1) % max(1, total)
    cycles = campaign.cycles + (1 if cursor == 0 else 0)
    await db.advance_campaign(
        campaign.id,
        cursor=cursor,
        next_run_at=int(time.time()) + _delay(campaign.interval),
        cycles=cycles,
        ok=ok,
    )


async def _notify(bot, user_id: int, text: str) -> None:
    """Сказать человеку, что с его рассылкой. Молча, если он закрыл личку."""
    if bot is None:
        return
    try:
        await bot.send_message(user_id, text)
    except Exception as error:
        log.debug("уведомление %s не ушло: %s", user_id, error)


async def run_one(bot, campaign: db.Campaign) -> None:
    """Одна отправка одной рассылки: проверки, сообщение, сдвиг круга."""
    now = int(time.time())

    subscription = await db.subscription(campaign.user_id)
    if not subscription.active:
        await db.set_campaign_status(
            campaign.id, "paused", "бесплатный период закончился"
        )
        await _notify(bot, campaign.user_id, texts.CAMPAIGN_EXPIRED)
        return

    account = await db.account(campaign.user_id, campaign.account_id)
    if account is None:
        await db.set_campaign_status(campaign.id, "stopped", "аккаунт отключён")
        return
    if account.status != "ok":
        await db.set_campaign_status(campaign.id, "paused", "аккаунт не в сети")
        await _notify(bot, campaign.user_id, texts.campaign_account_dead(campaign))
        return

    pause = await db.account_pause(account.id)
    if pause is not None:
        until, _ = pause
        await db.reschedule_campaign(campaign.id, until + 5)
        return

    # Общий разрыв между сообщениями аккаунта. Считается по журналу, а не
    # по этой рассылке: рассылок у аккаунта может быть несколько, а
    # Telegram смотрит на аккаунт.
    last = await db.last_send_at(account.id)
    if last and now - last < config.ACCOUNT_MIN_GAP:
        await db.reschedule_campaign(campaign.id, last + config.ACCOUNT_MIN_GAP)
        return

    if await db.sent_today(account.id) >= config.DAILY_LIMIT:
        await db.reschedule_campaign(campaign.id, now + 1800)
        if campaign.note != texts.NOTE_DAILY:
            await db.set_campaign_status(campaign.id, "running", texts.NOTE_DAILY)
            await _notify(bot, campaign.user_id, texts.daily_limit(account.title))
        return

    targets = await _targets(campaign)
    if not targets:
        await db.set_campaign_status(campaign.id, "paused", "список чатов пуст")
        await _notify(bot, campaign.user_id, texts.campaign_empty(campaign))
        return

    chat_id = targets[campaign.cursor % len(targets)]
    found = await db.chats_by_ids(account.id, [chat_id])
    if not found:
        # Чат пропал из списка — например, аккаунт из него вышел.
        # Круг едет дальше, чат отмечается в журнале.
        await db.log_send(
            campaign.id, account.id, chat_id, None, False, "чата нет в списке"
        )
        await _advance(campaign, len(targets), ok=False)
        return
    chat = found[0]

    live = await _live(account)
    try:
        async with live.lock:
            await _deliver(live.client, campaign, _input_peer(chat))
    except Exception as error:
        await _handle_error(bot, campaign, account, chat, error, len(targets))
        return

    await db.log_send(campaign.id, account.id, chat.chat_id, chat.title, True, None)
    await _advance(campaign, len(targets), ok=True)
    log.info(
        "рассылка %s: отправлено в «%s» (%s)",
        campaign.id, chat.title, account.title,
    )


async def _handle_error(
    bot,
    campaign: db.Campaign,
    account: db.Account,
    chat: db.Chat,
    error: Exception,
    total: int,
) -> None:
    if isinstance(error, MaterialGone):
        # Материал удалили из «Избранного». Круг продолжать нельзя: он
        # будет спотыкаться на каждом чате и копить ошибки.
        await db.log_send(
            campaign.id, account.id, chat.chat_id, chat.title, False,
            "материал удалён из «Избранного»",
        )
        await db.set_campaign_status(
            campaign.id, "paused", "материал удалён из «Избранного»"
        )
        await _notify(bot, campaign.user_id, texts.material_gone(campaign))
        return

    action, reason, seconds = classify(error)
    now = int(time.time())
    await db.log_send(
        campaign.id, account.id, chat.chat_id, chat.title, False, reason
    )

    if action == "flood":
        # Пауза вешается на аккаунт, а не на рассылку: ограничение выдано
        # аккаунту, и остальные его рассылки должны замолчать тоже.
        await db.pause_account(account.id, now + seconds, reason)
        await db.reschedule_campaign(campaign.id, now + seconds + 5)
        log.warning("аккаунт %s: FloodWait %s с", account.title, seconds)
        if seconds > 600:
            await _notify(
                bot, campaign.user_id, texts.flood_wait(account.title, seconds)
            )
        return

    if action == "slowmode":
        # Не наказание, а настройка чата: ждём столько, сколько просят.
        await db.reschedule_campaign(campaign.id, now + max(seconds, 30) + 5)
        return

    if action == "peerflood":
        # Самое серьёзное: Telegram счёл поведение аккаунта спамом.
        # Продолжать — значит идти к блокировке номера.
        await db.pause_account(account.id, now + 86400, reason)
        stopped = await db.stop_account_campaigns(account.id, reason)
        log.warning("аккаунт %s: PeerFlood, остановлено рассылок %s",
                    account.title, stopped)
        await _notify(bot, campaign.user_id, texts.peer_flood(account.title))
        return

    if action == "dead":
        await db.mark_account(account.id, "dead", reason)
        await db.stop_account_campaigns(account.id, reason)
        await _close(account.id)
        await _notify(bot, campaign.user_id, texts.campaign_account_dead(campaign))
        return

    if action == "unknown":
        log.warning(
            "рассылка %s, чат «%s»: %s", campaign.id, chat.title, reason
        )

    # skip и unknown: страдает один чат, круг едет дальше.
    await _advance(campaign, total, ok=False)


# --- цикл -------------------------------------------------------------


async def tick(bot) -> None:
    now = int(time.time())
    for campaign in await db.due_campaigns(now):
        try:
            await run_one(bot, campaign)
        except Exception:
            log.exception("рассылка %s сорвалась", campaign.id)
            # Минута тишины вместо мгновенного повтора: если сломалось
            # что-то наше, повтор в тот же тик сломается так же, только
            # в цикле и с полной нагрузкой.
            await db.reschedule_campaign(campaign.id, now + 60)


async def worker(bot) -> None:
    log.info(
        "движок рассылки: тик %s с, минимальный интервал %s с, "
        "разрыв на аккаунт %s с, потолок %s сообщений в сутки",
        config.BROADCAST_TICK,
        config.MIN_INTERVAL,
        config.ACCOUNT_MIN_GAP,
        config.DAILY_LIMIT,
    )
    while True:
        await asyncio.sleep(config.BROADCAST_TICK)
        try:
            await tick(bot)
            await close_idle()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("тик рассылки сорвался")
