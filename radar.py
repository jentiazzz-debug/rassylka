"""Радар заказов: аккаунт читает чаты и приносит человеку то, что нашёл.

**Чем это отличается от всего остального в боте.** Рассылка и
автокомментарий пишут. Радар не пишет никуда и ничего — он только
читает. Поэтому здесь нет ни пауз между сообщениями, ни суточного
потолка, ни разбора FloodWait: чтение чужих сообщений в чате, где
аккаунт состоит, для Telegram не событие. Единственный расходуемый
ресурс здесь — не репутация аккаунта, а деньги за разбор смыслов, и
весь учёт построен вокруг него.

**Почему первая фаза без отправки.** Прежде чем автоматизировать ответы,
надо знать, сколько заказов в сутки вообще дают выбранные чаты и какого
они качества. Радар отвечает на этот вопрос за сутки работы, и ответ
часто оказывается «три штуки» — а три штуки в сутки автоматизировать
нечем и незачем, хватает уведомления с готовой карточкой.

**Ловим двумя способами сразу**, ровно как автокомментарии: события дают
скорость (а скоростью радар и продаётся — заказ в чате забирают первые
три ответа), опрос раз в `RADAR_POLL` подбирает всё, что события
потеряли на разрыве подключения или перезапуске. Отметка прочитанного
общая, поэтому дважды одно сообщение в модель не уедет.

**Курсор двигается по факту прочтения, а не разбора.** Разбор идёт
пачками и с задержкой; ждать его, чтобы сдвинуть отметку, значило бы
разбирать одно и то же по кругу при каждом перезапуске — и платить за
это. Цена решения: сообщения, пойманные ровно в момент падения
процесса, теряются. Для ленты лидов это приемлемо, для отправки денег
было бы нет.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import broadcast
import classify
import config
import db
import texts

log = logging.getLogger("rassylka.radar")

#: На какие чаты уже висит обработчик: account_id -> {chat_id: radar_id}.
_listening: dict[int, dict[int, int]] = {}


@dataclass(slots=True)
class _Batch:
    """Накопитель сообщений одного радара."""

    items: list[classify.Candidate] = field(default_factory=list)
    #: Когда положили первое. По нему считается, не пора ли отправлять
    #: недобранную пачку: в тихом чате соседей можно ждать до вечера.
    opened_at: float = 0.0


_pending: dict[int, _Batch] = {}


# --- сбор -------------------------------------------------------------


def _author(message) -> tuple[int, str, str | None]:
    """Кто написал. Имя собирается из того, что есть.

    У части авторов нет ни фамилии, ни ника — только id. Карточка без
    имени бесполезна, поэтому пустое имя заменяется на что угодно
    осмысленное, вплоть до номера.
    """
    sender = getattr(message, "sender", None)
    author_id = int(getattr(message, "sender_id", 0) or 0)
    if sender is None:
        return author_id, f"id{author_id}" if author_id else "без имени", None
    parts = [
        getattr(sender, "first_name", "") or "",
        getattr(sender, "last_name", "") or "",
    ]
    name = " ".join(p for p in parts if p).strip()
    username = getattr(sender, "username", None)
    if not name:
        name = username or (f"id{author_id}" if author_id else "без имени")
    return author_id, name[:64], username


def _skip(message) -> bool:
    """Сообщения, которые до предфильтра доходить не должны.

    Свои — потому что радар не должен находить заказы в собственных
    сообщениях аккаунта. Ботов — потому что заказ от бота это либо
    пересланная реклама, либо агрегатор, и в обоих случаях писать
    некому.
    """
    if getattr(message, "out", False):
        return True
    if getattr(message, "action", None) is not None:
        return True
    sender = getattr(message, "sender", None)
    if sender is not None and getattr(sender, "bot", False):
        return True
    return False


async def _offer(radar: db.Radar, chat_title: str, chat_id: int, message) -> bool:
    """Прогнать сообщение через сито и положить в пачку, если прошло.

    Возвращает True, если сообщение дошло до пачки. Счётчики радара
    двигаются здесь же: по отношению «увидено / прошло сито» видно, что
    ключевые слова подобраны мимо, задолго до того, как это заметит
    человек по пустой ленте.
    """
    text = (getattr(message, "text", None) or "").strip()
    if _skip(message) or not text:
        return False

    # Счётчики двигаются одной записью, а не двумя: в живом чате это
    # сотни сообщений в час, и каждая лишняя фиксация транзакции здесь
    # оплачивается на ровном месте.
    taken = classify.prefilter(text, radar.keywords)
    await db.bump_radar(radar.id, seen=1, passed=1 if taken else 0)
    if not taken:
        return False

    author_id, author_name, author_user = _author(message)
    batch = _pending.setdefault(radar.id, _Batch())
    if not batch.items:
        batch.opened_at = time.monotonic()
    batch.items.append(
        classify.Candidate(
            chat_id=chat_id,
            chat_title=chat_title,
            msg_id=int(message.id),
            author_id=author_id,
            author_name=author_name,
            author_user=author_user,
            text=text,
        )
    )
    return True


# --- разбор пачек -----------------------------------------------------


async def _flush_due(bot) -> None:
    """Отправить в разбор пачки, которые набрались или заждались."""
    now = time.monotonic()
    for radar_id, batch in list(_pending.items()):
        if not batch.items:
            continue
        full = len(batch.items) >= config.RADAR_BATCH
        stale = now - batch.opened_at >= config.RADAR_BATCH_WAIT
        if not (full or stale):
            continue
        items = batch.items[: config.RADAR_BATCH]
        del batch.items[: config.RADAR_BATCH]
        batch.opened_at = now
        try:
            await _run_batch(bot, radar_id, items)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("разбор пачки радара %s сорвался", radar_id)


async def _run_batch(bot, radar_id: int, items: list[classify.Candidate]) -> None:
    radar = await db.radar_by_id(radar_id)
    if radar is None or radar.status != "running":
        return

    # Потолок сторожит деньги, а не работу. Когда модели нет, тратить
    # нечего: разбор идёт регулярками, и упираться в лимит, которого не
    # существует, радар не должен.
    live = classify.ready()
    calls, _ = await db.radar_usage_today(radar.user_id)
    if live and calls >= config.RADAR_DAILY_CALLS:
        # Потолок не глушит радар: сбор продолжается, разбор смыслов
        # встаёт до завтра. Человеку об этом говорим один раз за сутки —
        # повторять на каждой пачке значит спамить самому.
        if calls == config.RADAR_DAILY_CALLS:
            await db.bump_radar_usage(radar.user_id, 1, 0)
            await _tell(bot, radar.user_id, texts.radar_quota_hit())
        return

    verdicts = await classify.classify(radar.profile, items)
    if live:
        await db.bump_radar_usage(radar.user_id, 1, len(items))

    floor = radar.min_score or config.RADAR_MIN_SCORE
    for item, verdict in zip(items, verdicts):
        await _store(bot, radar, item, verdict, floor)


async def _store(bot, radar: db.Radar, item: classify.Candidate,
                 verdict: classify.Verdict, floor: int) -> None:
    """Записать разбор и, если заказ стоящий, показать его человеку.

    Отвергнутое тоже пишется: без него нельзя посмотреть, что именно
    классификатор выбрасывает, и настроить профиль вслепую не выйдет.
    """
    # Планка оценки применяется только к разбору модели. Оценка от
    # регулярок — не суждение, а сила сигнала, и мерить её той же
    # линейкой нельзя: поднятая планка молча обнулила бы всю ленту у
    # всех, у кого ключа нет. Ровно этим режим без модели и ломается —
    # снаружи такое неотличимо от «в чатах просто нет заказов».
    # Фильтр по бюджету остаётся: он про названные цифры, а не про
    # чьё-то мнение, и работает одинаково в обоих режимах.
    keep = (
        verdict.order
        and (not verdict.judged or verdict.score >= floor)
        and (not radar.min_budget or not verdict.budget
             or verdict.budget >= radar.min_budget)
    )
    lead, fresh = await db.save_lead(
        radar.id,
        radar.user_id,
        classify.fingerprint(item.author_id, item.text),
        {
            "chat_id": item.chat_id,
            "chat_title": item.chat_title,
            "msg_id": item.msg_id,
            "author_id": item.author_id,
            "author_name": item.author_name,
            "author_user": item.author_user,
            "text": item.text,
            "score": verdict.score,
            "budget": verdict.budget,
            "currency": verdict.currency,
            "urgency": verdict.urgency,
            "stack": verdict.stack,
            "summary": verdict.summary,
            "verdict": "order" if keep else "skip",
            "judged": verdict.judged,
        },
    )
    if not keep or not fresh:
        # Повтор того же заказа в соседнем чате человеку не показываем:
        # счётчик в базе вырос, карточка уже была. Именно повторы и
        # превращают ленту лидов в то, от чего она спасает.
        return

    await db.bump_radar(radar.id, found=1)
    link = await _link(radar, item)
    await _tell(bot, radar.user_id, texts.lead_card(lead, link))
    await db.mark_lead_notified(lead.id)


async def _link(radar: db.Radar, item: classify.Candidate) -> str | None:
    """Ссылка прямо на сообщение в чате.

    Ради неё карточка и нужна: заказ выигрывается скоростью, и человек
    должен попадать в нужное сообщение одним нажатием, а не искать его
    в чате руками.
    """
    try:
        found = await db.chats_by_ids(radar.account_id, [item.chat_id])
    except Exception as error:
        log.debug("чат %s не нашёлся: %s", item.chat_id, error)
        return None
    if not found:
        return None
    chat = found[0]
    if chat.username:
        return f"https://t.me/{chat.username}/{item.msg_id}"
    # Приватная супергруппа: в ссылке идёт внутренний id без приставки.
    return f"https://t.me/c/{chat.raw_id}/{item.msg_id}"


async def _tell(bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(
            user_id, text, disable_web_page_preview=True
        )
    except Exception as error:
        # Человек мог заблокировать бота — радар из-за этого вставать не
        # должен, находки продолжат копиться в базе.
        log.debug("не доставил находку %s: %s", user_id, error)


# --- слушатели --------------------------------------------------------


async def ensure_listeners(bot) -> None:
    """Привести подписки в соответствие с включёнными радарами.

    Вызывается на каждом тике: радары включают и выключают из
    приложения, а узнать об этом иначе процессу неоткуда.
    """
    from telethon import events

    want: dict[int, dict[int, int]] = {}
    titles: dict[int, str] = {}
    for radar in await db.radars_running():
        for chat in await db.radar_chats(radar.id):
            want.setdefault(radar.account_id, {})[chat.chat_id] = radar.id
            titles[chat.chat_id] = chat.title

    for account_id in list(_listening):
        if account_id not in want:
            _listening.pop(account_id, None)
            live = broadcast._clients.get(account_id)
            if live is not None:
                # Снимать keep можно только если аккаунт не занят ещё и
                # автокомментарием: там своё живое подключение, и отнять
                # его у соседа радар не вправе.
                live.keep = await _still_needed(account_id)

    for account_id, chats in want.items():
        known = _listening.setdefault(account_id, {})
        fresh = {cid: rid for cid, rid in chats.items() if cid not in known}
        known.clear()
        known.update(chats)

        # Флаг «не закрывать» подтверждается на каждом тике, а не только
        # когда появились новые чаты. Причина в соседе: автокомментарий
        # на том же аккаунте при выключении сбивает keep в False
        # безусловно, и радар остался бы без подключения через
        # CLIENT_IDLE — причём молча, потому что слушатели уехали бы
        # вместе с подключением, а радар остался бы «включённым».
        cached = broadcast._clients.get(account_id)
        if cached is not None:
            cached.keep = True

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
                _on_message(bot, account_id, chat_id, titles.get(chat_id, "")),
                events.NewMessage(chats=chat_id),
            )
            log.info("радар слушает чат %s аккаунтом %s", chat_id, account_id)


async def _still_needed(account_id: int) -> bool:
    """Нужно ли подключение кому-то ещё, кроме радара."""
    watch_rows = await db.watches_running()
    return any(watch.account_id == account_id for watch in watch_rows)


def _on_message(bot, account_id: int, chat_id: int, chat_title: str):
    """Обработчик нового сообщения в чате. Замыкание, а не метод: Telethon
    хранит обработчики по объекту функции, и общий метод пришлось бы
    различать вручную."""

    async def handler(event) -> None:
        radar_id = _listening.get(account_id, {}).get(chat_id)
        if radar_id is None:
            return
        radar = await db.radar_by_id(radar_id)
        if radar is None or radar.status != "running":
            return
        try:
            await _offer(radar, chat_title or str(chat_id), chat_id, event.message)
            await db.set_radar_cursor(radar_id, chat_id, int(event.message.id))
        except Exception:
            log.exception("радар споткнулся на сообщении %s", event.message.id)

    return handler


# --- опрос страховкой -------------------------------------------------


async def poll_radar(radar: db.Radar) -> int:
    """Подобрать из чатов радара всё, что пропустили события."""
    chats = await db.radar_chats(radar.id)
    if not chats:
        return 0

    account = await db.account_by_id(radar.account_id)
    if account is None or account.status != "ok":
        await db.set_radar_status(
            radar.id, "stopped", "аккаунт отвалился — радар остановлен"
        )
        return 0

    live = await broadcast._live(account)
    known = {c.chat_id: c for c in await db.chats_by_ids(
        radar.account_id, [c.chat_id for c in chats]
    )}

    taken = 0
    for chat in chats:
        source = known.get(chat.chat_id)
        if source is None:
            continue
        try:
            async with live.lock:
                messages = await live.client.get_messages(
                    broadcast._input_peer(source),
                    limit=config.RADAR_CATCHUP,
                    min_id=chat.last_msg_id,
                )
        except Exception as error:
            log.debug("чат %s не читается: %s", chat.chat_id, error)
            continue

        newest = chat.last_msg_id
        # От старого к новому: пачка должна собираться в том порядке, в
        # каком заказы появлялись, иначе свежий уедет в разбор последним.
        for message in sorted(messages, key=lambda m: m.id):
            newest = max(newest, int(message.id))
            if await _offer(radar, chat.title, chat.chat_id, message):
                taken += 1
        if newest > chat.last_msg_id:
            await db.set_radar_cursor(radar.id, chat.chat_id, newest)
    return taken


async def tick(bot) -> None:
    try:
        await ensure_listeners(bot)
    except Exception:
        log.exception("подписка радара сорвалась")

    for radar in await db.radars_running():
        try:
            await poll_radar(radar)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("опрос радара %s сорвался", radar.id)


async def worker(bot) -> None:
    """Главный цикл.

    Два разных ритма в одном цикле, а не в двух задачах: пачки надо
    проверять часто (секунды решают), чаты — редко (каждый заход это
    запрос к Telegram от лица аккаунта). Разносить их по задачам ради
    этого незачем, а общий цикл пришлось бы будить с частотой самого
    нетерпеливого из двух.
    """
    log.info(
        "радар: события плюс опрос раз в %s с, пачка до %s сообщений или "
        "%.1f с, разбор моделью %s, планка %s",
        config.RADAR_POLL,
        config.RADAR_BATCH,
        config.RADAR_BATCH_WAIT,
        config.RADAR_MODEL if classify.ready() else "выключен (нет ключа)",
        config.RADAR_MIN_SCORE,
    )
    next_poll = 0.0
    while True:
        await asyncio.sleep(0.5)
        try:
            await _flush_due(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("разбор пачек сорвался")

        if time.monotonic() >= next_poll:
            next_poll = time.monotonic() + config.RADAR_POLL
            try:
                await tick(bot)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("тик радара сорвался")
