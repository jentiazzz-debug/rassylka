"""Автокомментарий: аккаунт отвечает под новыми постами канала.

Зачем это отдельно от рассылки. У рассылки есть круг по чатам и
интервал: движок сам решает, когда и куда писать. Здесь решает не
движок, а канал — вышел пост, появился повод ответить. Нет поста —
никто никуда не пишет, и в этом вся разница: рассылка идёт по
расписанию, а комментарий приходит на событие.

**Как ловим пост — двумя способами сразу, и это не перестраховка.**

*События.* На каждый аккаунт с включённым наблюдением держится живое
подключение с обработчиком новых сообщений канала. Ответ уходит в ту же
секунду — ради этого всё и затевалось: раздачи «первым десяти»
выигрываются секундами, и любая пауза здесь означает, что подарки
разберут без нас.

*Опрос* остаётся страховкой. Подключение рвётся, процесс
перезапускается, Telegram молчит — событие теряется, и без опроса пост
остался бы неотвеченным навсегда. Опрос раз в `COMMENT_POLL` секунд
подбирает всё, что пропустили события; отметка последнего поста общая,
поэтому дважды один пост не комментируется.

**Задержка по умолчанию нулевая.** Ответ через паузу выглядит
человечнее, и в вилке `delay_min…delay_max` это по-прежнему можно
включить. Но по умолчанию мы отвечаем сразу: выбор между
«правдоподобно» и «успеть» здесь сделан в пользу «успеть» — за этим
функцию и просили.

**Обсуждение появляется не мгновенно.** Группа обсуждений подхватывает
свежий пост с задержкой в доли секунды, и ответ в ту же миллисекунду
Telegram встречает словами «нет такого сообщения». Поэтому попытка не
одна: `COMMENT_RETRIES` заходов с паузой `COMMENT_RETRY_PAUSE`. Без них
мгновенный режим ломался бы ровно на самых быстрых постах — тех, ради
которых он и сделан.

**Куда именно пишем.** В группу обсуждений канала, ответом на пост —
`comment_to` у Telethon делает ровно это: сам спрашивает у Telegram
связанное сообщение и отвечает на него в linked-чате. Аккаунт при этом
должен быть в группе обсуждений; если он туда не вступал, Telegram
откажет, и наблюдение честно встанет с объяснением.

Тормоза те же, что у рассылки, и по той же причине: Telegram считает
сообщения аккаунта, а не наши затеи. Разрыв между сообщениями и суточный
потолок общие — журнал `sends` один на аккаунт.

**Один пост — один ответ.** Отметка `last_msg_id` двигается и при
ошибке: иначе пост, на который ответить не вышло, застрял бы в очереди
навсегда и не дал бы обработать следующие.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import broadcast
import config
import db
import texts

log = logging.getLogger("rassylka.comments")


async def _post_ids(client, chat: db.Chat, after: int) -> list[int]:
    """Номера новых постов канала, от старого к новому.

    Берём не больше `COMMENT_CATCHUP` за раз: если бота не было сутки, а
    канал постил каждый час, отвечать разом на все двадцать постов —
    это ровно тот всплеск, за который аккаунт и ограничивают.
    """
    peer = broadcast._input_peer(chat)
    found = []
    async for message in client.iter_messages(peer, limit=config.COMMENT_CATCHUP):
        if message.id <= after:
            break
        # Служебные записи (закрепили, изменили фото) постами не
        # считаются: комментировать там нечего.
        if getattr(message, "action", None) is not None:
            continue
        found.append(message.id)
    return sorted(found)


async def newest_post(account, chat: db.Chat) -> int:
    """Номер последнего поста канала — точка отсчёта для наблюдения.

    Отвечать задним числом на посты, которые вышли до того, как человек
    включил автокомментарии, никто не просил: под старым постом свежий
    комментарий выглядит некстати, а под раздачей, которая закончилась
    вчера, — глупо.
    """
    live = await broadcast._live(account)
    async with live.lock:
        found = await live.client.get_messages(
            broadcast._input_peer(chat), limit=1
        )
    return int(found[0].id) if found else 0


#: Отказы, которые значат «обсуждение ещё не подхватило пост». Их ждут,
#: а не считают ошибкой: через секунду то же самое пройдёт.
NOT_READY = {"MsgIdInvalidError", "MessageIdInvalidError"}


async def _reply(client, watch: db.Watch, chat: db.Chat, post_id: int) -> None:
    """Оставить один комментарий под постом, дождавшись обсуждения.

    Вариант выбирается тем же способом, что и в рассылке: один и тот же
    текст под каждым постом ловится проверкой на совпадение так же
    легко, как и в чатах.
    """
    for attempt in range(config.COMMENT_RETRIES):
        try:
            await _send(client, watch, chat, post_id)
            return
        except Exception as error:
            if (type(error).__name__ not in NOT_READY
                    or attempt == config.COMMENT_RETRIES - 1):
                raise
            # Пост есть, а обсуждения под ним ещё нет. Это норма на
            # свежем посте, и ждать тут дешевле, чем отдавать его в
            # ошибки: следующий заход опроса будет только через минуту.
            log.debug(
                "обсуждение поста %s ещё не готово, попытка %s",
                post_id, attempt + 1,
            )
            await asyncio.sleep(config.COMMENT_RETRY_PAUSE)


async def _send(client, watch: db.Watch, chat: db.Chat, post_id: int) -> None:
    """Одна попытка отправки — без повторов."""
    items = await db.watch_variants(watch.id)
    if not items:
        raise RuntimeError("нет ни одного варианта ответа")

    if len(items) == 1:
        variant = items[0]
    elif watch.pick == "order":
        position = watch.text_cursor % len(items)
        await db.set_watch_cursor(watch.id, (position + 1) % len(items))
        variant = items[position]
    else:
        variant = random.choice(items)

    peer = broadcast._input_peer(chat)

    if variant.get("content") != "saved" or not variant.get("saved_id"):
        text = await broadcast.compose(watch.user_id, variant.get("text") or "")
        await client.send_message(peer, text, comment_to=post_id, parse_mode=None)
        return

    # Материал перечитывается перед каждой отправкой: у файлов есть
    # file_reference, он живёт считанные часы, и сохранённая ссылка через
    # сутки перестаёт работать.
    source = await client.get_messages("me", ids=variant["saved_id"])
    if source is None:
        raise broadcast.MaterialGone()

    if (getattr(source, "sticker", None) is not None
            or getattr(source, "video_note", None) is not None):
        await client.send_file(peer, source.media, comment_to=post_id)
        return

    own = (variant.get("text") or "").strip()
    entities = None if own else (source.entities or None)
    caption = await broadcast.compose(watch.user_id, own or source.message or "")

    if source.media is not None:
        await client.send_file(
            peer,
            source.media,
            caption=caption or None,
            formatting_entities=entities,
            comment_to=post_id,
            parse_mode=None,
        )
        return

    await client.send_message(
        peer,
        caption,
        formatting_entities=entities,
        comment_to=post_id,
        parse_mode=None,
    )


async def run_one(bot, watch: db.Watch, post_id: int | None = None) -> None:
    """Ответить на пост канала.

    `post_id` приходит из события — тогда в канал за списком постов
    ходить незачем. Без него это обычный заход опросом: посмотреть, не
    вышло ли чего нового.
    """
    now = int(time.time())

    subscription = await db.subscription(watch.user_id)
    if not subscription.active:
        await db.set_watch_status(watch.id, "paused", "бесплатный период закончился")
        await broadcast._notify(bot, watch.user_id, texts.CAMPAIGN_EXPIRED)
        return

    account = await db.account(watch.user_id, watch.account_id)
    if account is None:
        await db.set_watch_status(watch.id, "stopped", "аккаунт отключён")
        return
    if account.status != "ok":
        await db.set_watch_status(watch.id, "paused", "аккаунт не в сети")
        return

    pause = await db.account_pause(account.id)
    if pause is not None:
        until, _ = pause
        await db.reschedule_watch(watch.id, until + 5)
        return

    found = await db.chats_by_ids(account.id, [watch.chat_id])
    if not found:
        await db.set_watch_status(watch.id, "paused", "канала нет в списке чатов")
        return
    chat = found[0]

    live = await broadcast._live(account)
    if post_id is None:
        try:
            async with live.lock:
                posts = await _post_ids(live.client, chat, watch.last_msg_id)
        except Exception as error:
            await _handle_error(bot, watch, account, chat, error)
            return
        if not posts:
            await db.reschedule_watch(watch.id, now + config.COMMENT_POLL)
            return
        post_id = posts[0]

    # Разрыв между сообщениями аккаунта и суточный потолок — общие с
    # рассылкой: Telegram смотрит на аккаунт, а не на то, чем мы его
    # заняли. Пост при этом никуда не денется: отметка не сдвигается, и
    # следующий заход возьмёт его же.
    last = await db.last_send_at(account.id)
    if last and now - last < config.ACCOUNT_MIN_GAP:
        await db.reschedule_watch(watch.id, last + config.ACCOUNT_MIN_GAP)
        return
    if await db.sent_today(account.id) >= config.DAILY_LIMIT:
        await db.reschedule_watch(watch.id, now + 1800)
        if watch.note != texts.NOTE_DAILY:
            await db.set_watch_status(watch.id, "running", texts.NOTE_DAILY)
        return

    pause = _delay(watch)
    if pause:
        # Ждём до отправки, а не после: иначе пауза сдвигала бы не ответ,
        # а следующий заход опроса, и «отвечать через минуту» означало бы
        # «отвечать сразу, но раз в минуту».
        await asyncio.sleep(pause)

    try:
        async with live.lock:
            await _reply(live.client, watch, chat, post_id)
    except broadcast.MaterialGone:
        await db.set_watch_status(
            watch.id, "paused", "материал пропал из «Избранного»"
        )
        await broadcast._notify(
            bot, watch.user_id, texts.watch_material_gone(watch)
        )
        return
    except Exception as error:
        await db.mark_watch_seen(watch.id, post_id, ok=False)
        await _handle_error(bot, watch, account, chat, error)
        return

    # Журнал отправок общий на аккаунт — по нему считаются разрыв между
    # сообщениями и суточный потолок, а Telegram смотрит именно на
    # аккаунт. Рассылки у этой записи нет, поэтому campaign_id = 0:
    # журнал рассылки читается по её номеру и такие записи не подхватит.
    await db.log_send(0, account.id, chat.chat_id, chat.title, True, None)
    await db.mark_watch_seen(watch.id, post_id, ok=True)
    await db.reschedule_watch(watch.id, int(time.time()) + config.COMMENT_POLL)
    log.info(
        "автокомментарий %s: ответ под постом %s в «%s»",
        watch.id, post_id, chat.title,
    )


def _delay(watch: db.Watch) -> int:
    """Через сколько отвечать. Ноль — сразу.

    Вилка задаётся наблюдением, но ниже общей планки не опускается: она
    на случай, когда владелец бота решил, что мгновенные ответы ему
    дороже успетых раздач.
    """
    low = max(config.COMMENT_MIN_DELAY, watch.delay_min)
    high = max(low, watch.delay_max)
    return low if low == high else random.randint(low, high)


async def _handle_error(bot, watch: db.Watch, account, chat, error) -> None:
    """Что делать с отказом Telegram. Разбор общий с рассылкой."""
    action, reason, seconds = broadcast.classify(error)

    if action == "dead":
        await db.mark_account(account.id, "dead", reason)
        await db.stop_account_watches(account.id, reason)
        await db.stop_account_campaigns(account.id, reason)
        await broadcast._notify(
            bot, watch.user_id, texts.watch_account_dead(watch)
        )
        return

    if action == "peerflood":
        await db.pause_account(
            account.id, int(time.time()) + config.PEERFLOOD_PAUSE, reason
        )
        await db.set_watch_status(watch.id, "paused", reason)
        await broadcast._notify(
            bot, watch.user_id, texts.peer_flood(account.title)
        )
        return

    if action == "flood":
        await db.pause_account(account.id, int(time.time()) + seconds, reason)
        await db.reschedule_watch(watch.id, int(time.time()) + seconds + 5)
        return

    if action == "slowmode":
        await db.reschedule_watch(watch.id, int(time.time()) + seconds + 5)
        return

    if action == "skip":
        # Чаще всего это «аккаунт не в группе обсуждений» или у канала её
        # вовсе нет. Молчать нельзя: человек будет думать, что
        # комментарии идут.
        await db.set_watch_status(watch.id, "paused", reason)
        await broadcast._notify(bot, watch.user_id, texts.watch_stuck(watch, reason))
        log.info("автокомментарий %s встал: %s", watch.id, reason)
        return

    log.warning("автокомментарий %s: %s (%s)", watch.id, reason, type(error).__name__)
    await db.reschedule_watch(watch.id, int(time.time()) + config.COMMENT_POLL)


# --- мгновенный режим: подписка на новые посты ------------------------

#: Какие каналы уже слушает аккаунт: {account_id: {chat_id: watch_id}}.
#: Нужно, чтобы не вешать второй обработчик на тот же канал и снимать
#: подписку, когда наблюдение выключили.
_listening: dict[int, dict[int, int]] = {}


async def ensure_listeners(bot) -> None:
    """Привести подписки в соответствие с включёнными наблюдениями.

    Вызывается на каждом тике: наблюдения включают и выключают из
    приложения, а узнать об этом иначе процессу неоткуда.
    """
    from telethon import events

    want: dict[int, dict[int, int]] = {}
    for watch in await db.watches_running():
        want.setdefault(watch.account_id, {})[watch.chat_id] = watch.id

    # Ушедшие: аккаунт, у которого не осталось наблюдений, больше не
    # обязан держать подключение живым.
    for account_id in list(_listening):
        if account_id not in want:
            _listening.pop(account_id, None)
            live = broadcast._clients.get(account_id)
            if live is not None:
                live.keep = False

    for account_id, channels in want.items():
        known = _listening.setdefault(account_id, {})
        fresh = {chat_id: wid for chat_id, wid in channels.items()
                 if chat_id not in known}
        known.clear()
        known.update(channels)
        if not fresh:
            continue

        account = await db.account_by_id(account_id)
        if account is None or account.status != "ok":
            _listening.pop(account_id, None)
            continue

        try:
            live = await broadcast._live(account)
        except Exception as error:
            log.warning("не подключиться к аккаунту %s: %s", account_id, error)
            _listening.pop(account_id, None)
            continue
        live.keep = True

        for chat_id in fresh:
            live.client.add_event_handler(
                _on_post(bot, account_id, chat_id),
                events.NewMessage(chats=chat_id),
            )
            log.info("слушаю посты канала %s аккаунтом %s", chat_id, account_id)


def _on_post(bot, account_id: int, chat_id: int):
    """Обработчик нового поста. Замыкание, а не метод: Telethon хранит
    обработчики по объекту функции, и общий метод пришлось бы различать
    вручную."""

    async def handler(event) -> None:
        # Пост мог прийти в канал, наблюдение за которым уже выключили:
        # обработчик снимается не мгновенно.
        if _listening.get(account_id, {}).get(chat_id) is None:
            return
        watch = await db.watch_by_chat(account_id, chat_id)
        if watch is None or watch.status != "running":
            return
        if event.id <= watch.last_msg_id:
            return
        try:
            await run_one(bot, watch, post_id=event.id)
        except Exception:
            log.exception("мгновенный ответ на пост %s сорвался", event.id)

    return handler


async def tick(bot) -> None:
    try:
        await ensure_listeners(bot)
    except Exception:
        log.exception("подписка на посты сорвалась")

    now = int(time.time())
    for watch in await db.due_watches(now):
        try:
            await run_one(bot, watch)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("автокомментарий %s сорвался", watch.id)
            await db.reschedule_watch(watch.id, now + config.COMMENT_POLL)


async def worker(bot) -> None:
    log.info(
        "автокомментарии: ответ по событию, опрос страховкой раз в %s с, "
        "отставание не больше %s постов, задержка от %s с",
        config.COMMENT_POLL,
        config.COMMENT_CATCHUP,
        config.COMMENT_MIN_DELAY,
    )
    while True:
        await asyncio.sleep(config.BROADCAST_TICK)
        try:
            await tick(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("тик автокомментариев сорвался")
