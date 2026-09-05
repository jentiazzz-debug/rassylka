"""Самопроверка: база, шифрование, подпись initData, ручки API.

Запуск: python selfcheck.py

Telegram здесь не участвует — ни бот, ни MTProto. Проверяется то, что
можно сломать правкой кода и не заметить глазами:

* пробный период выдаётся один раз, а не при каждом /start;
* повторный вход по тому же номеру обновляет аккаунт, а не плодит копии;
* сессия в базе зашифрована, и наружу, в мини-апп, не уходит;
* подделанная, просроченная и чужая initData не проходят;
* API без подписи отвечает отказом, а не отдаёт данные.

Последнее — главное. Ручки мини-аппа доступны из интернета, и вся
защита там держится на проверке подписи: сломав её, бот начнёт отдавать
чужие номера любому желающему, и по внешнему виду это не заметно.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path

import config

# Своя база и свой ключ: селфчек не должен трогать боевые сессии.
_temp = Path(tempfile.mkdtemp(prefix="rassylka-check-"))
config.DB_PATH = _temp / "check.db"
config.SESSION_KEY_FILE = _temp / "session.key"
config.SESSION_KEY = ""
# Токен нужен для подписи initData. Боевой сюда не подставляем: подпись
# считается тем же алгоритмом, и для проверки логики хватает любого.
config.BOT_TOKEN = "123456:TEST-TOKEN-FOR-SELFCHECK"
config.TRIAL_DAYS = 5
config.MAX_ACCOUNTS = 2

import accounts  # noqa: E402  — только после подмены путей
import broadcast  # noqa: E402
import chats  # noqa: E402
import crypto  # noqa: E402
import db  # noqa: E402
import handlers  # noqa: E402
import keyboards  # noqa: E402
import legal  # noqa: E402
import payments  # noqa: E402
import platega  # noqa: E402
import tdata  # noqa: E402
import texts  # noqa: E402
import webapp  # noqa: E402

# Консоль Windows по умолчанию не в UTF-8, и один эмодзи в отчёте роняет
# весь селфчек с UnicodeEncodeError.
try:
    sys.stdout.reconfigure(errors="replace")
except (AttributeError, ValueError):
    pass

PASS = "  [ ok ]"
FAIL = "  [FAIL]"
_failed = 0

USER = 555001
OTHER = 555002


def check(name: str, ok: bool, detail: str = "") -> None:
    global _failed
    if ok:
        print(f"{PASS} {name}")
    else:
        _failed += 1
        print(f"{FAIL} {name}" + (f" — {detail}" if detail else ""))


def init_data(user_id: int, *, auth_date: int | None = None,
              signature: bool = False, token: str | None = None) -> str:
    """Собрать initData так, как её собирает клиент Telegram."""
    fields = {
        "user": json.dumps(
            {"id": user_id, "first_name": "Тест", "username": "test"},
            separators=(",", ":"),
            ensure_ascii=False,
        ),
        "auth_date": str(auth_date if auth_date is not None else int(time.time())),
        "query_id": "AAH-test",
    }
    if signature:
        fields["signature"] = "ed25519-подпись-телеграма"
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(
        b"WebAppData", (token or config.BOT_TOKEN).encode(), hashlib.sha256
    ).digest()
    fields["hash"] = hmac.new(
        secret, check_string.encode(), hashlib.sha256
    ).hexdigest()
    return urllib.parse.urlencode(fields)


# --- проверки ---------------------------------------------------------


async def check_crypto() -> None:
    print("\nШифрование сессий")
    check("ключ создаётся сам", crypto.ready())
    secret = "1BQANOTEuMTA4LjU2LjE0MAG7uDEsK1lZ0A=="
    blob = crypto.encrypt(secret)
    check("шифротекст не равен исходнику", blob != secret)
    check("расшифровывается обратно", crypto.decrypt(blob) == secret)
    check("чужой шифротекст не ломает процесс", crypto.decrypt("мусор") is None)


async def check_trial() -> None:
    print("\nПробный период")
    await db.ensure_user(USER, "test", "Тест")
    first = await db.subscription(USER)
    check("после первого /start идёт триал", first.kind == "trial", first.kind)
    check(
        f"дней в триале: {first.days_left}",
        first.days_left == config.TRIAL_DAYS,
        str(first.days_left),
    )

    # Второй /start не должен выдавать пять дней заново.
    await db.ensure_user(USER, "test2", "Тест")
    again = await db.subscription(USER)
    check("повторный /start не продлевает триал", again.until == first.until)

    # Кончившийся триал.
    await db._conn().execute(
        "UPDATE users SET trial_ends_at = ? WHERE user_id = ?",
        (int(time.time()) - 10, USER),
    )
    await db._conn().commit()
    over = await db.subscription(USER)
    check("кончившийся триал — expired", over.kind == "expired", over.kind)
    check("expired неактивен", not over.active)

    # Оплата поверх кончившегося триала.
    until = await db.grant_paid(USER, 30)
    paid = await db.subscription(USER)
    check("оплата даёт paid", paid.kind == "paid", paid.kind)
    check("оплата активна", paid.active and paid.paid)
    # Продление считается от даты конца, а не от «сейчас».
    later = await db.grant_paid(USER, 30)
    check("продление не съедает остаток", later > until)

    # Возвращаем триал: дальше он нужен живым.
    await db._conn().execute(
        "UPDATE users SET trial_ends_at = ?, paid_until = 0 WHERE user_id = ?",
        (int(time.time()) + config.TRIAL_DAYS * 86400, USER),
    )
    await db._conn().commit()


async def check_accounts() -> None:
    print("\nАккаунты в базе")
    session = crypto.encrypt("session-строка-1")
    first_id = await db.save_account(
        USER, "+79990000001", session, tg_id=1, name="Первый", username="one"
    )
    check("аккаунт записался", first_id > 0)

    # Повторный вход по тому же номеру — обновление, а не копия.
    same_id = await db.save_account(
        USER,
        "+79990000001",
        crypto.encrypt("session-строка-2"),
        tg_id=1,
        name="Первый",
        username="one",
    )
    check("тот же номер не плодит записи", same_id == first_id)
    check("аккаунтов у человека: 1", await db.count_accounts(USER) == 1)

    stored = await db.account(USER, first_id)
    check("сессия обновилась", crypto.decrypt(stored.session) == "session-строка-2")
    check(
        "в базе лежит шифротекст, а не сессия",
        "session-строка" not in stored.session,
    )

    # Чужой аккаунт по номеру записи достать нельзя.
    check("чужой аккаунт не отдаётся", await db.account(OTHER, first_id) is None)
    check(
        "и не удаляется",
        not await db.delete_account(OTHER, first_id),
    )

    await db.mark_account(first_id, "dead", "сессия отозвана")
    dead = await db.account(USER, first_id)
    check("статус пишется", dead.status == "dead" and dead.note == "сессия отозвана")

    # Список для мини-аппа не должен содержать сессию.
    view = webapp._account_view(dead)
    check("сессия не уходит в мини-апп", "session" not in view)

    check(
        "лимит аккаунтов виден",
        await db.count_accounts(USER) < config.MAX_ACCOUNTS,
    )
    check("аккаунт удаляется", await db.delete_account(USER, first_id))


async def check_throttle() -> None:
    print("\nТормоз на запросы кода")
    check("счётчик пуст", await db.code_requests_last_hour(OTHER) == 0)
    for _ in range(3):
        await db.log_code_request(OTHER, "+79990000009")
    check("запросы считаются", await db.code_requests_last_hour(OTHER) == 3)

    # Ключи подставляем фиктивные: тормоз обязан срабатывать до похода в
    # Telegram, поэтому до сети этот вызов дойти не должен. Если дойдёт —
    # проверка провалится по таймауту или сетевой ошибке, и это ровно тот
    # сигнал, который нужен.
    config.MTPROTO_API_ID, config.MTPROTO_API_HASH = 1, "проверка"
    config.CODE_REQUESTS_PER_HOUR = 3
    try:
        await accounts.start(OTHER, "+79990000009")
        check("лимит запросов кода срабатывает", False, "исключения не было")
    except accounts.LoginError as error:
        check("лимит запросов кода срабатывает", "час" in error.message.lower(),
              error.message)
    finally:
        config.CODE_REQUESTS_PER_HOUR = 5
        config.MTPROTO_API_ID, config.MTPROTO_API_HASH = 0, ""

    # А без ключей — отказ до всякой сети и до тормоза.
    try:
        await accounts.start(USER, "+79990000009")
        check("без ключей MTProto вход не начинается", False, "исключения не было")
    except accounts.LoginError as error:
        check("без ключей MTProto вход не начинается",
              "поддержку" in error.message, error.message)


def check_phones() -> None:
    print("\nРазбор номера")
    cases = {
        "+7 999 123-45-67": "+79991234567",
        # Домашняя запись: кода страны 8 не существует, это +7.
        "89991234567": "+79991234567",
        "8 (999) 123-45-67": "+79991234567",
        "+1 (415) 555-2671": "+14155552671",
        "": "",
        "123": "",
        "не номер": "",
    }
    for raw, expect in cases.items():
        got = accounts.normalize_phone(raw)
        check(f"{raw!r} → {got!r}", got == expect, f"ожидалось {expect!r}")


def check_init_data() -> None:
    print("\nПодпись initData")
    good = webapp.parse_init_data(init_data(USER))
    check("своя подпись проходит", good is not None and good["id"] == USER)

    with_signature = webapp.parse_init_data(init_data(USER, signature=True))
    check("вариант с полем signature проходит", with_signature is not None)

    check("пустая строка не проходит", webapp.parse_init_data("") is None)
    check(
        "без hash не проходит",
        webapp.parse_init_data("user=%7B%22id%22%3A1%7D&auth_date=1") is None,
    )

    # Подмена данных при живой подписи — главный сценарий, ради которого
    # проверка и нужна: id меняют на чужой, hash оставляют.
    fields = dict(urllib.parse.parse_qsl(init_data(USER)))
    fields["user"] = json.dumps({"id": OTHER, "first_name": "Чужой"})
    check(
        "подмена user не проходит",
        webapp.parse_init_data(urllib.parse.urlencode(fields)) is None,
    )

    check(
        "чужой токен не проходит",
        webapp.parse_init_data(init_data(USER, token="999:OTHER")) is None,
    )

    old = init_data(USER, auth_date=int(time.time()) - config.INITDATA_TTL - 60)
    check("просроченная не проходит", webapp.parse_init_data(old) is None)


async def check_api() -> None:
    print("\nРучки мини-аппа")
    from aiohttp.test_utils import TestClient, TestServer

    client = TestClient(TestServer(webapp.build()))
    await client.start_server()
    try:
        page = await client.get("/")
        check("страница отдаётся", page.status == 200)
        body = await page.text()
        check("это мини-апп", "telegram-web-app.js" in body)
        check("без кэша", page.headers.get("Cache-Control") == "no-store")

        css = await client.get("/app.css")
        check("стили отдаются", css.status == 200)
        js = await client.get("/app.js")
        check("скрипт отдаётся", js.status == 200)
        # Каталог наружу не отдаём: файлы перечислены явно.
        sneak = await client.get("/app.py")
        check("посторонние файлы не отдаются", sneak.status == 404)

        blank = await client.post("/api/state", json={})
        check("состояние без подписи: отказ", blank.status == 401)

        for path in (
            "/api/login/start",
            "/api/login/code",
            "/api/login/password",
            "/api/account/verify",
            "/api/account/forget",
            "/api/account/tdata",
            "/api/profile",
            "/api/invoice",
            "/api/materials",
            "/api/subscribe",
            "/api/campaign/edit",
        ):
            answer = await client.post(path, json={"phone": "+79990000000", "id": 1})
            check(f"{path} без подписи: отказ", answer.status == 401)

        signed = await client.post(
            "/api/state", json={}, headers={webapp.INIT_HEADER: init_data(USER)}
        )
        check("состояние по подписи: 200", signed.status == 200)
        state = await signed.json()
        check("состояние пришло", state.get("ok") is True)
        check(
            "видно подписку",
            state["subscription"]["kind"] in {"trial", "paid", "expired"},
            str(state.get("subscription")),
        )
        check("видно лимиты", state["limits"]["max_accounts"] == config.MAX_ACCOUNTS)
        check(
            "MTProto не настроен — так и сказано",
            state["mtproto_ready"] == config.mtproto_ready(),
        )

        # Подпись в теле, а не в заголовке: некоторые прокси срезают
        # нестандартные заголовки, и этот путь должен работать тоже.
        in_body = await client.post(
            "/api/state", json={"init_data": init_data(USER)}
        )
        check("подпись в теле тоже принимается", in_body.status == 200)

        # Человек, открывший приложение по прямой ссылке, минуя /start,
        # должен заводиться сам — иначе у него не будет триала.
        fresh_id = 555777
        fresh = await client.post(
            "/api/state", json={}, headers={webapp.INIT_HEADER: init_data(fresh_id)}
        )
        fresh_state = await fresh.json()
        check(
            "новый человек заводится при открытии приложения",
            fresh_state["subscription"]["kind"] == "trial",
            str(fresh_state["subscription"]),
        )

        # Чужой токен входа завершить нельзя.
        stolen = await client.post(
            "/api/login/code",
            json={"token": "подобранный-токен", "code": "12345"},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )
        answer = await stolen.json()
        check("чужой токен входа не проходит", answer.get("ok") is False)

        # Без ключей MTProto запрос кода должен вежливо отказать, а не
        # уронить ручку пятисоткой.
        no_keys = await client.post(
            "/api/login/start",
            json={"phone": "+79990001234"},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )
        check("запрос кода без ключей не роняет сервер", no_keys.status == 200)
        body = await no_keys.json()
        check("и объясняет причину", body.get("ok") is False, str(body))

        health = await client.get("/health")
        check("health отвечает", health.status == 200)
    finally:
        await client.close()


# --- рассылка ---------------------------------------------------------


class FakeClient:
    """Клиент, который никуда не ходит и запоминает отправленное.

    Ради него весь движок и проверяется целиком: круг по чатам, паузы,
    лимиты и разбор ошибок — это ровно та логика, которую нельзя
    проверить глазами и нельзя гонять на живом Telegram.
    """

    def __init__(self) -> None:
        self.sent: list[tuple] = []
        self.fail: Exception | None = None

    def is_connected(self) -> bool:
        return True

    async def send_message(self, peer, text, parse_mode=None):
        if self.fail is not None:
            error, self.fail = self.fail, None
            raise error
        self.sent.append((peer, text))

    async def disconnect(self) -> None:
        pass


FAKE = FakeClient()


async def _fake_live(account):
    return broadcast._Live(client=FAKE, used_at=0.0)


async def _make_campaign(account_id: int, chat_ids: list[int],
                         interval: int = 0) -> db.Campaign:
    campaign_id = await db.create_campaign(
        USER, account_id,
        title="Тест", text="привет", interval=interval,
        source="chats", folder_id=None, chat_ids=chat_ids,
        start_at=int(time.time()) - 1,
    )
    return await db.campaign(USER, campaign_id)


async def check_broadcast() -> None:
    print("\nДвижок рассылки")

    # Тормоза на время проверки снимаем: они проверяются отдельно, а
    # здесь мешают прогнать круг за один заход.
    config.MIN_INTERVAL = 0
    config.ACCOUNT_MIN_GAP = 0
    config.INTERVAL_JITTER = 0
    config.FREE_FOOTER = "Рассылка бесплатно — @тестбот"
    broadcast._live = _fake_live

    account_id = await db.save_account(
        USER, "+79990000100", crypto.encrypt("s"), tg_id=7,
        name="Отправитель", username="sender",
    )
    await db.save_chats(account_id, [
        {"chat_id": -1001, "raw_id": 1, "access_hash": 11, "kind": "channel",
         "broadcast": False, "title": "Первый чат", "username": None},
        {"chat_id": -1002, "raw_id": 2, "access_hash": 22, "kind": "channel",
         "broadcast": False, "title": "Второй чат", "username": None},
        {"chat_id": 3003, "raw_id": 3, "access_hash": 33, "kind": "user",
         "broadcast": False, "title": "Человек", "username": "man"},
    ])
    check("чаты записались", len(await db.chats(account_id)) == 3)

    # Повторное сканирование заменяет список, а не дописывает.
    await db.save_chats(account_id, [
        {"chat_id": -1001, "raw_id": 1, "access_hash": 11, "kind": "channel",
         "broadcast": False, "title": "Первый чат", "username": None},
        {"chat_id": -1002, "raw_id": 2, "access_hash": 22, "kind": "channel",
         "broadcast": False, "title": "Второй чат", "username": None},
        {"chat_id": 3003, "raw_id": 3, "access_hash": 33, "kind": "user",
         "broadcast": False, "title": "Человек", "username": "man"},
    ])
    check("пересканирование не плодит чаты",
          len(await db.chats(account_id)) == 3)

    targets = [-1001, -1002, 3003]
    campaign = await _make_campaign(account_id, targets)

    # Главное: круг идёт по очереди и заворачивается на начало.
    FAKE.sent.clear()
    order = []
    for _ in range(4):
        fresh = await db.campaign(USER, campaign.id)
        order.append(targets[fresh.cursor % len(targets)])
        await broadcast.run_one(None, fresh)

    check(f"пишет по очереди: {order}", order == [-1001, -1002, 3003, -1001],
          str(order))
    check("ушло четыре сообщения", len(FAKE.sent) == 4, str(len(FAKE.sent)))

    after = await db.campaign(USER, campaign.id)
    check("круг засчитан", after.cycles == 1, str(after.cycles))
    check("счётчик удачных сходится", after.sent_ok == 4, str(after.sent_ok))

    # Подпись бесплатного тарифа.
    check("на бесплатном дописывается подпись",
          FAKE.sent[0][1].endswith(config.FREE_FOOTER), FAKE.sent[0][1])
    check("сам текст на месте", FAKE.sent[0][1].startswith("привет"))

    await db.grant_paid(USER, 30)
    paid_text = await broadcast.compose(USER, "привет")
    check("на платном подписи нет", paid_text == "привет", paid_text)
    await db._conn().execute(
        "UPDATE users SET paid_until = 0 WHERE user_id = ?", (USER,)
    )
    await db._conn().commit()

    # Адресат собирается из базы, без обращения к Telegram.
    peer = broadcast._input_peer((await db.chats_by_ids(account_id, [-1001]))[0])
    check("канал уходит как InputPeerChannel",
          type(peer).__name__ == "InputPeerChannel", type(peer).__name__)
    check("и с сохранённым access_hash", peer.access_hash == 11)
    human = broadcast._input_peer((await db.chats_by_ids(account_id, [3003]))[0])
    check("личка уходит как InputPeerUser",
          type(human).__name__ == "InputPeerUser", type(human).__name__)

    await db.delete_campaign(USER, campaign.id)


async def check_broadcast_errors() -> None:
    print("\nОшибки Telegram при отправке")

    account_id = (await db.accounts(USER))[0].id
    targets = [-1001, -1002, 3003]

    class ChatWriteForbiddenError(Exception):
        pass

    class FloodWaitError(Exception):
        def __init__(self):
            self.seconds = 300

    class PeerFloodError(Exception):
        pass

    class AuthKeyUnregisteredError(Exception):
        pass

    action, _, _ = broadcast.classify(ChatWriteForbiddenError())
    check("нельзя писать в чат — пропускаем чат", action == "skip", action)
    action, _, seconds = broadcast.classify(FloodWaitError())
    check("FloodWait — пауза аккаунта", action == "flood" and seconds == 300,
          f"{action}/{seconds}")
    action, _, _ = broadcast.classify(PeerFloodError())
    check("PeerFlood — остановка", action == "peerflood", action)
    action, _, _ = broadcast.classify(AuthKeyUnregisteredError())
    check("отозванная сессия — аккаунт мёртв", action == "dead", action)

    # Чат, в который нельзя писать, не должен ронять рассылку: круг
    # обязан ехать дальше, а не встать на этом чате навсегда.
    campaign = await _make_campaign(account_id, targets)
    FAKE.fail = ChatWriteForbiddenError()
    await broadcast.run_one(None, campaign)
    after = await db.campaign(USER, campaign.id)
    check("после отказа круг едет дальше", after.cursor == 1, str(after.cursor))
    check("ошибка посчитана", after.sent_err == 1, str(after.sent_err))
    journal = await db.sends(campaign.id)
    check("причина попала в журнал",
          journal and journal[0]["error"] == "нельзя писать в этот чат",
          str(journal[:1]))

    # FloodWait обязан молчать аккаунтом, а не одной рассылкой.
    FAKE.fail = FloodWaitError()
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    pause = await db.account_pause(account_id)
    check("FloodWait поставил аккаунт на паузу", pause is not None)
    check("пауза примерно на 300 с",
          pause and 250 < pause[0] - int(time.time()) <= 300, str(pause))

    # Пока аккаунт на паузе, рассылка не отправляет ничего.
    before = len(FAKE.sent)
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    check("на паузе аккаунт молчит", len(FAKE.sent) == before)

    await db._conn().execute("DELETE FROM account_pauses")
    await db._conn().commit()

    # PeerFlood останавливает все рассылки аккаунта.
    FAKE.fail = PeerFloodError()
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    stopped = await db.campaign(USER, campaign.id)
    check("PeerFlood остановил рассылку", stopped.status == "stopped",
          stopped.status)
    await db._conn().execute("DELETE FROM account_pauses")
    await db._conn().commit()
    await db.delete_campaign(USER, campaign.id)


async def check_broadcast_limits() -> None:
    print("\nЛимиты рассылки")

    account_id = (await db.accounts(USER))[0].id
    campaign = await _make_campaign(account_id, [-1001, -1002])

    # Разрыв между сообщениями аккаунта. Нужна предыстория: первому
    # сообщению ждать нечего, разрыв считается от предыдущего — и
    # проверять надо именно это, а не пустой журнал.
    await db.log_send(campaign.id, account_id, -1001, "Первый чат", True, None)
    config.ACCOUNT_MIN_GAP = 3600
    before = len(FAKE.sent)
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    check("разрыв между сообщениями соблюдается", len(FAKE.sent) == before)
    moved = await db.campaign(USER, campaign.id)
    check("отправка перенесена вперёд", moved.next_run_at > int(time.time()))
    config.ACCOUNT_MIN_GAP = 0

    # Дневной лимит.
    config.DAILY_LIMIT = 1
    await db.reschedule_campaign(campaign.id, int(time.time()) - 1)
    before = len(FAKE.sent)
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    check("дневной лимит держит", len(FAKE.sent) == before)
    noted = await db.campaign(USER, campaign.id)
    check("причина видна человеку", noted.note == texts.NOTE_DAILY, str(noted.note))
    config.DAILY_LIMIT = 200

    # Кончившаяся подписка ставит рассылку на паузу.
    await db._conn().execute(
        "UPDATE users SET trial_ends_at = ?, paid_until = 0 WHERE user_id = ?",
        (int(time.time()) - 10, USER),
    )
    await db._conn().commit()
    await db.set_campaign_status(campaign.id, "running", None)
    await db.reschedule_campaign(campaign.id, int(time.time()) - 1)
    await broadcast.run_one(None, await db.campaign(USER, campaign.id))
    expired = await db.campaign(USER, campaign.id)
    check("без подписки рассылка встаёт", expired.status == "paused",
          expired.status)
    await db._conn().execute(
        "UPDATE users SET trial_ends_at = ? WHERE user_id = ?",
        (int(time.time()) + 86400, USER),
    )
    await db._conn().commit()

    # Минимальный интервал не обойти: даже нулевой поднимается до планки.
    config.MIN_INTERVAL = 60
    check("интервал меньше минимума поднимается",
          broadcast._delay(1) >= 60, str(broadcast._delay(1)))
    check("заданный интервал сохраняется",
          540 <= broadcast._delay(600) <= 660, str(broadcast._delay(600)))

    await db.delete_campaign(USER, campaign.id)


async def check_folders() -> None:
    print("\nПапки")
    account_id = (await db.accounts(USER))[0].id
    await db.save_folders(account_id, [
        {"folder_id": 2, "title": "Работа", "chat_ids": [-1001, 3003]},
        {"folder_id": 3, "title": "Пусто", "chat_ids": []},
    ])
    found = await db.folders(account_id)
    check("папки читаются", len(found) == 2, str(len(found)))
    work = await db.folder(account_id, 2)
    check("состав папки на месте", work.chat_ids == [-1001, 3003],
          str(work.chat_ids))

    # Рассылка по папке берёт состав заново при каждой отправке: чат,
    # добавленный в папку позже, должен попасть в круг сам.
    campaign_id = await db.create_campaign(
        USER, account_id, title="По папке", text="привет", interval=0,
        source="folder", folder_id=2, chat_ids=[],
        start_at=int(time.time()) - 1,
    )
    campaign = await db.campaign(USER, campaign_id)
    check("цели берутся из папки",
          await broadcast._targets(campaign) == [-1001, 3003])

    await db.save_folders(account_id, [
        {"folder_id": 2, "title": "Работа", "chat_ids": [-1001, 3003, -1002]},
    ])
    check("изменение папки подхватывается",
          await broadcast._targets(campaign) == [-1001, 3003, -1002])
    await db.delete_campaign(USER, campaign_id)


async def check_tdata() -> None:
    print("\nИмпорт из tdata")
    import shutil
    import zipfile

    check("библиотека разбора на месте", tdata.available())

    work = _temp / "tdzip"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)

    # Архив с путями, вырывающимися наружу. Имя внутри zip — просто
    # строка, и «../» в ней при наивной распаковке пишет мимо папки.
    evil = work / "evil.zip"
    with zipfile.ZipFile(evil, "w") as bundle:
        bundle.writestr("../сбежал.txt", "не должен появиться")
        bundle.writestr("../../тоже-сбежал.txt", "и этот")
        bundle.writestr("/абсолютный.txt", "и этот")
        bundle.writestr("tdata/key_data", "притворяюсь ключом")

    target = work / "out"
    target.mkdir()
    tdata._safe_extract(evil, target)
    escaped = [
        (work / "сбежал.txt").exists(),
        (work.parent / "тоже-сбежал.txt").exists(),
        (target / "абсолютный.txt").exists(),
    ]
    check("пути с ../ наружу не пишут", not any(escaped), str(escaped))
    check("нормальный файл распаковался", (target / "tdata" / "key_data").exists())

    # tdata ищется по файлу ключа, как бы её ни заархивировали.
    check("tdata находится вложенной", tdata.find_tdata(target) == target / "tdata")
    check("и когда заархивировали её саму",
          tdata.find_tdata(target / "tdata") == target / "tdata")
    empty = work / "пусто"
    empty.mkdir()
    check("и не находится там, где её нет", tdata.find_tdata(empty) is None)

    # Потолки размера и числа файлов.
    saved_size, saved_count = tdata.MAX_UNPACKED, tdata.MAX_ENTRIES
    tdata.MAX_UNPACKED = 5
    try:
        tdata._safe_extract(evil, target)
        check("архив-бомба отбивается", False, "исключения не было")
    except tdata.TdataError as error:
        check("архив-бомба отбивается", "большой" in error.message, error.message)
    finally:
        tdata.MAX_UNPACKED = saved_size

    tdata.MAX_ENTRIES = 1
    try:
        tdata._safe_extract(evil, target)
        check("слишком много файлов отбивается", False, "исключения не было")
    except tdata.TdataError as error:
        check("слишком много файлов отбивается", "много" in error.message,
              error.message)
    finally:
        tdata.MAX_ENTRIES = saved_count

    # Не-архив.
    junk = work / "не-архив.zip"
    junk.write_text("просто текст", encoding="utf-8")
    try:
        tdata._safe_extract(junk, target)
        check("не-zip отбивается", False, "исключения не было")
    except tdata.TdataError as error:
        check("не-zip отбивается", "zip" in error.message.lower(), error.message)

    # Архив без tdata внутри — понятный отказ, а не падение.
    plain = work / "чужой.zip"
    with zipfile.ZipFile(plain, "w") as bundle:
        bundle.writestr("фото.jpg", "не тдата")
    try:
        await tdata.import_zip(USER, plain)
        check("архив без tdata отбивается", False, "исключения не было")
    except tdata.TdataError as error:
        check("архив без tdata отбивается", "tdata" in error.message.lower(),
              error.message)

    # После импорта временных папок остаться не должно: там лежат ключи
    # от чужих аккаунтов.
    leftovers = list(config.DATA_DIR.glob("tdata-*"))
    check("временные папки убираются", not leftovers, str(leftovers))

    # Тексты ошибок opentele переводятся на человеческий.
    class PasswordIncorrect(Exception):
        pass

    class TFileNotFound(Exception):
        pass

    check("неверный код-пароль объясняется",
          "код-пароль" in tdata._explain(PasswordIncorrect()).lower())
    check("не-tdata объясняется",
          "tdata" in tdata._explain(TFileNotFound()).lower())


async def check_account_api() -> None:
    print("\nКлючи аккаунта из tdata")

    desktop = {
        "api_id": 2040,
        "api_hash": "b18441a1ff607e10a989891a5462e627",
        "device_model": "Desktop",
        "system_version": "Windows 11",
        "app_version": "4.9.0 x64",
        "lang_code": "en",
        "system_lang_code": "en-US",
    }
    account_id = await db.save_account(
        USER, "+79990000200", crypto.encrypt("s"), tg_id=9,
        name="Из tdata", username=None, api=desktop, source="tdata",
    )
    saved = await db.account(USER, account_id)
    check("способ подключения запомнен", saved.source == "tdata", saved.source)
    check("ключи запомнены", saved.api == desktop, str(saved.api))

    # Главное: такой аккаунт обязан ходить своими ключами. Подключиться
    # к чужой сессии нашим api_id — верный способ её потерять.
    params = accounts._api_params(saved)
    check("подключение идёт ключами из tdata", params["api_id"] == 2040,
          str(params["api_id"]))
    check("и устройством из tdata", params["device_model"] == "Desktop",
          params["device_model"])

    # А аккаунт со входом по номеру — общими из .env.
    config.MTPROTO_API_ID, config.MTPROTO_API_HASH = 777, "наш-хэш"
    phone_id = await db.save_account(
        USER, "+79990000201", crypto.encrypt("s"), tg_id=10,
        name="По номеру", username=None,
    )
    by_phone = await db.account(USER, phone_id)
    check("вход по номеру — без своих ключей", by_phone.api is None)
    check("и подключается общими",
          accounts._api_params(by_phone)["api_id"] == 777)
    config.MTPROTO_API_ID, config.MTPROTO_API_HASH = 0, ""

    # Перезапись записи не должна терять ключи: verify() пересохраняет
    # аккаунт целиком, и без передачи api он бы сломался после проверки.
    await db.save_account(
        USER, "+79990000200", saved.session, tg_id=9,
        name="Из tdata", username=None, api=saved.api, source=saved.source,
    )
    again = await db.account(USER, account_id)
    check("пересохранение не теряет ключи", again.api == desktop, str(again.api))

    await db.delete_account(USER, account_id)
    await db.delete_account(USER, phone_id)


async def check_payments() -> None:
    print("\nМонеты, подписка и оплата")
    check("тарифы разобрались", len(config.PLANS) == 3, str(config.PLANS))
    check("день — 30 монет", payments.plan_price(1) == 30)
    check("неделя — 100 монет", payments.plan_price(7) == 100)
    check("месяц — 200 монет", payments.plan_price(30) == 200)
    check("чужого тарифа нет", payments.plan_price(3) is None)
    # Пачки и скидки.
    check("пачки разобрались", len(config.COIN_PACKS) == 4,
          str(config.COIN_PACKS))
    check("обычная пачка по курсу", payments.pack_price(100) == 100)
    check("пачка со скидкой дешевле", payments.pack_price(200) == 190)
    check("и скидка видна", payments.base_price(200) == 200)
    check("500 со скидкой", payments.pack_price(500) == 460)

    # Своё количество: в границах можно, за границами нельзя.
    check("пачка продаётся", payments.sellable(100))
    check("своё количество в границах продаётся", payments.sellable(777))
    check("ниже нижней границы нельзя",
          not payments.sellable(config.COIN_MIN - 1))
    check("выше верхней нельзя", not payments.sellable(config.COIN_MAX + 1))
    check("ноль нельзя", not payments.sellable(0))
    check("отрицательное нельзя", not payments.sellable(-100))
    check("своё количество по обычному курсу",
          payments.pack_price(777) == 777 * config.STARS_PER_COIN)

    check("payload читается", payments.coins_from(payments.payload_for(100)) == 100)
    for junk in ("", "coins", "coins:", "coins:abc", "other:7", "7"):
        check(f"мусорный payload {junk!r} отбивается",
              payments.coins_from(junk) == 0)

    # Покупка подписки за монеты.
    ok, error = await payments.buy_subscription(USER, 7)
    check("без монет подписку не купить", not ok, error)
    check("и объясняем почему", "не хватает" in error.lower(), error)

    balance = await db.add_coins(USER, 150, "проверка")
    check("монеты начислились", balance == 150, str(balance))

    before = await db.subscription(USER)
    ok, error = await payments.buy_subscription(USER, 7)
    check("с монетами подписка покупается", ok, error)
    after = await db.subscription(USER)
    check("подписка продлилась", after.until > before.until)
    check("тариф стал платным", after.paid, after.kind)
    check("монеты списались", await db.coins_of(USER) == 50,
          str(await db.coins_of(USER)))

    # Списание не должно уходить в минус.
    ok, _ = await payments.buy_subscription(USER, 30)
    check("в минус не уходим", not ok)
    check("баланс не тронут", await db.coins_of(USER) == 50)

    # Журнал монет.
    history = await db.coin_history(USER)
    check("движения пишутся в журнал", len(history) >= 2, str(len(history)))
    check("списание отрицательное",
          any(op["delta"] < 0 for op in history), str(history[:2]))

    # Оплата звёздами: повтор не зачисляется дважды.
    fresh = await db.record_payment("charge-1", USER, 100, 100, "coins:100")
    check("платёж записался", fresh)
    again = await db.record_payment("charge-1", USER, 100, 100, "coins:100")
    check("тот же платёж дважды не зачтётся", not again)

    profile = await db.profile(USER)
    check("монеты видны в профиле", profile["coins"] == 50, str(profile))
    check("звёзды посчитаны", profile["stars"] == 100, str(profile))

    # Возвращаем триал: дальше он нужен живым.
    await db._conn().execute(
        "UPDATE users SET paid_until = 0, trial_ends_at = ? WHERE user_id = ?",
        (int(time.time()) + 86400, USER),
    )
    await db._conn().commit()


async def check_referrals() -> None:
    print("\nПриглашения")
    guest = 555900
    await db.ensure_user(guest, "guest", "Гость")

    check("ссылка разбирается",
          handlers.ref_from(handlers.ref_payload(USER)) == USER)
    for junk in ("", "r", "rabc", "12345", "x123"):
        check(f"мусорный payload {junk!r} отбивается",
              handlers.ref_from(junk) == 0)

    check("пригласивший записался", await db.set_referrer(guest, USER))
    check("и читается обратно", await db.referrer_of(guest) == USER)

    # Переприсвоить приглашённого нельзя: иначе чужая ссылка забирала бы
    # себе уже приглашённого человека.
    check("второй раз не переписать", not await db.set_referrer(guest, OTHER))
    check("пригласивший тот же", await db.referrer_of(guest) == USER)

    # Себя пригласить нельзя, и несуществующего тоже.
    lone = 555901
    await db.ensure_user(lone, None, None)
    check("сам себя не пригласишь", not await db.set_referrer(lone, lone))
    check("несуществующий не пригласит",
          not await db.set_referrer(lone, 999999999))

    before = await db.coins_of(USER)
    await db.add_coins(USER, config.REF_COINS, "приглашённый пришёл по ссылке")
    stats = await db.referral_stats(USER)
    check("приглашённые считаются", stats["invited"] == 1, str(stats))
    check("заработок считается", stats["earned"] == config.REF_COINS, str(stats))
    check("монеты пришли", await db.coins_of(USER) == before + config.REF_COINS)

    # Доля с пополнения приглашённого.
    was = await db.coins_of(USER)
    await payments.reward_referrer(None, guest, 100)
    share = 100 * config.REF_PERCENT // 100
    check(f"доля {config.REF_PERCENT}% начислена",
          await db.coins_of(USER) == was + share,
          str(await db.coins_of(USER)))

    # У человека без пригласившего доля никому не капает.
    total = await db.coins_of(USER)
    await payments.reward_referrer(None, lone, 100)
    check("без пригласившего доли нет", await db.coins_of(USER) == total)


async def check_materials() -> None:
    print("\nМатериалы из «Избранного»")
    account_id = await db.save_account(
        USER, "+79990000300", crypto.encrypt("s"), tg_id=11,
        name="С материалами", username=None,
    )
    await db.save_materials(account_id, [
        {"msg_id": 10, "kind": "photo", "preview": "Фото", "has_media": True},
        {"msg_id": 11, "kind": "text", "preview": "Привет", "has_media": False},
        {"msg_id": 12, "kind": "sticker", "preview": "Стикер", "has_media": True},
    ])
    found = await db.materials(account_id)
    check("материалы записались", len(found) == 3, str(len(found)))
    check("свежие сверху", found[0]["msg_id"] == 12, str(found[0]))
    check("тип сохраняется", found[0]["kind"] == "sticker")
    check("материал ищется по id", (await db.material(account_id, 11))["preview"]
          == "Привет")
    check("чужого материала нет", await db.material(account_id, 99) is None)

    # Пересканирование заменяет список: сообщение могли удалить.
    await db.save_materials(account_id, [
        {"msg_id": 10, "kind": "photo", "preview": "Фото", "has_media": True},
    ])
    check("пересканирование чистит пропавшее",
          len(await db.materials(account_id)) == 1)

    # Рассылка материалом.
    campaign_id = await db.create_campaign(
        USER, account_id, title="С фото", text="", interval=0,
        source="chats", folder_id=None, chat_ids=[-1001],
        start_at=int(time.time()), content="saved", saved_id=10,
    )
    campaign = await db.campaign(USER, campaign_id)
    check("рассылка помнит материал",
          campaign.content == "saved" and campaign.saved_id == 10,
          f"{campaign.content}/{campaign.saved_id}")

    # Правка: текст, интервал, переключение на текст.
    await db.edit_campaign(
        USER, campaign_id, title="Уже текстом", text="новый текст",
        interval=300, content="text", saved_id=None,
    )
    edited = await db.campaign(USER, campaign_id)
    check("текст поменялся", edited.text == "новый текст", edited.text)
    check("интервал поменялся", edited.interval == 300, str(edited.interval))
    check("вернулись к тексту", edited.content == "text", edited.content)
    check("название поменялось", edited.title == "Уже текстом", edited.title)

    # Круг и счётчики правка не сбрасывает: поправить опечатку — не повод
    # писать заново в те чаты, куда уже написали.
    await db.advance_campaign(campaign_id, cursor=1, next_run_at=0, cycles=0,
                              ok=True)
    await db.edit_campaign(
        USER, campaign_id, title="Ещё раз", text="другой текст",
        interval=300, content="text", saved_id=None,
    )
    kept = await db.campaign(USER, campaign_id)
    check("правка не сбрасывает круг", kept.cursor == 1, str(kept.cursor))
    check("и не сбрасывает счётчики", kept.sent_ok == 1, str(kept.sent_ok))

    check("чужую рассылку не поправить",
          not await db.edit_campaign(OTHER, campaign_id, title="взлом",
                                     text="x", interval=300, content="text",
                                     saved_id=None))

    await db.delete_campaign(USER, campaign_id)
    await db.delete_account(USER, account_id)


async def check_admin() -> None:
    print("\nАдминка")
    from aiohttp.test_utils import TestClient, TestServer

    config.ADMIN_IDS = {USER}
    client = TestClient(TestServer(webapp.build()))
    await client.start_server()
    try:
        paths = ("/api/admin/find", "/api/admin/user", "/api/admin/grant",
                 "/api/admin/campaign", "/api/admin/stats")

        # Чужому админские ручки не должны даже подтверждать своё
        # существование: отвечаем 404, а не 403.
        for path in paths:
            answer = await client.post(
                path, json={"id": USER},
                headers={webapp.INIT_HEADER: init_data(OTHER)},
            )
            check(f"{path} чужому: не отдаётся", answer.status == 404,
                  str(answer.status))

        # Без подписи вообще — отказ по подписи.
        for path in paths:
            answer = await client.post(path, json={})
            check(f"{path} без подписи: отказ", answer.status == 401,
                  str(answer.status))

        # Своему — открыто.
        stats = await client.post(
            "/api/admin/stats", json={},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )
        check("сводка админу отдаётся", stats.status == 200)
        body = await stats.json()
        check("в сводке есть люди", "users" in body.get("stats", {}), str(body))

        # Поиск по id и по нику.
        by_id = await (await client.post(
            "/api/admin/find", json={"query": str(USER)},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )).json()
        check("поиск по id находит",
              any(u["id"] == USER for u in by_id.get("users", [])), str(by_id))

        # Карточка.
        card = await (await client.post(
            "/api/admin/user", json={"id": USER},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )).json()
        check("карточка собирается", card.get("ok") is True, str(card)[:120])
        for key in ("user", "subscription", "accounts", "campaigns",
                    "coins", "payments", "invoices", "sends"):
            check(f"в карточке есть {key}", key in card["card"])

        # Выдача монет и её след в журнале.
        before = await db.coins_of(USER)
        grant = await (await client.post(
            "/api/admin/grant", json={"id": USER, "coins": 25},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )).json()
        check("монеты выдаются", grant.get("ok") is True, str(grant))
        check("баланс вырос", await db.coins_of(USER) == before + 25)
        history = await db.coin_history(USER, 3)
        check("выдача помечена в журнале",
              any("поддержки" in op["reason"] for op in history), str(history))

        # Списание отрицательным числом.
        await client.post(
            "/api/admin/grant", json={"id": USER, "coins": -25},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )
        check("минус списывает", await db.coins_of(USER) == before)

        # Защита от опечатки в поле.
        huge = await (await client.post(
            "/api/admin/grant", json={"id": USER, "coins": 10 ** 9},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )).json()
        check("нелепое число отбивается", huge.get("ok") is False, str(huge))
        check("баланс не тронут", await db.coins_of(USER) == before)

        # Несуществующий человек.
        nobody = await (await client.post(
            "/api/admin/grant", json={"id": 999999999, "coins": 5},
            headers={webapp.INIT_HEADER: init_data(USER)},
        )).json()
        check("несуществующему не выдаётся", nobody.get("ok") is False,
              str(nobody))
    finally:
        await client.close()
        config.ADMIN_IDS = set()


async def check_legal() -> None:
    print("\nДокументы")
    config.LEGAL_NAME = "ИП Иванов Иван Иванович"
    config.LEGAL_INN = "770000000000"
    config.LEGAL_EMAIL = "support@example.com"
    config.LEGAL_DATE = "01.09.2026"
    config.LEGAL_SHOW_REQUISITES = False
    config.REVIEW_CODE = "mellivora"

    for path, render in legal.RENDERERS.items():
        html = render()
        check(f"{path} собирается", "<h1>" in html and "</html>" in html)
        check(f"{path} с датой редакции", "01.09.2026" in html, path)
        check(f"{path} без незаполненных мест", 'class="todo"' not in html,
              path)

        # Банк на согласовании просил убрать ИП/ООО/ИНН из бота и
        # документов — проверяем, что их там нет ни в каком виде.
        check(f"{path} без ИНН", "ИНН" not in html, path)
        check(f"{path} без имени ИП", "Иванов" not in html, path)

        # И что кодовое слово проверяющего на месте.
        check(f"{path} с кодовым словом", "mellivora" in html, path)

        # Контакты поддержки банк требует отдельно — они остаются.
        check(f"{path} с контактом поддержки",
              "support@example.com" in html or path == "/tariffs", path)

    # Про продажу аккаунтов, номеров, почт и соцсетей в документах не
    # должно быть ни слова: это отдельное замечание банка.
    for path, render in legal.RENDERERS.items():
        low = render().lower()
        for word in ("продажа номеров", "продam", "куплю аккаунт",
                     "продажа аккаунт", "продам"):
            check(f"{path} без «{word}»", word not in low, path)

    # Реквизиты включаются обратно одной переменной, без правки текстов.
    config.LEGAL_SHOW_REQUISITES = True
    check("по флагу реквизиты возвращаются", "ИНН" in legal.terms())
    config.LEGAL_SHOW_REQUISITES = False

    # Кодовое слово убирается очисткой переменной — тоже без правок.
    config.REVIEW_CODE = ""
    check("без REVIEW_CODE слова нигде нет",
          all("mellivora" not in render() for render in legal.RENDERERS.values()))
    check("и в боте тоже нет", texts.review_line() == "")
    config.REVIEW_CODE = "mellivora"
    check("в боте слово появляется", "mellivora" in texts.review_line())

    # Тарифы обязаны совпадать с конфигом.
    page = legal.tariffs()
    for plan in config.PLANS:
        check(f"тариф {plan['days']} дн. в документе",
              f"{plan['stars']} {config.COIN_NAME}" in page, str(plan))
    for pack in config.COIN_PACKS:
        check(f"пачка {pack['coins']} за звёзды в документе",
              f"{pack['stars']} ⭐" in page, str(pack))
    for pack in config.RUB_PACKS:
        check(f"пачка {pack['coins']} за рубли в документе",
              f"{pack['rub']:.0f} ₽" in page, str(pack))
    check("лимиты в документе",
          str(config.DAILY_LIMIT) in page and str(config.MIN_INTERVAL) in page)
    check("бесплатный период в документе", str(config.TRIAL_DAYS) in page)
    check("порядок возврата описан", "возврат" in legal.terms().lower())
    check("что храним — описано",
          "ключ авторизации" in legal.privacy().lower())


async def check_menu_buttons() -> None:
    print("\nКнопки документов в боте")
    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"

    buttons = [b for row in keyboards.main_menu().inline_keyboard for b in row]
    urls = {b.url for b in buttons if b.url}

    # Документы должны быть отдельными кнопками, а не за командой:
    # проверяющий из банка не станет искать /terms в списке команд.
    for path in ("/terms", "/privacy", "/tariffs", "/support"):
        check(f"кнопка на {path} есть",
              f"https://example.com{path}" in urls, str(sorted(urls)))
    check("кнопка приложения на месте",
          any(b.web_app for b in buttons))
    check("кнопка поддержки на месте", "https://t.me/support" in urls)

    # Без адреса приложения кнопок документов быть не может — вести
    # некуда, и битая кнопка хуже отсутствующей.
    config.WEBAPP_URL = ""
    plain = [b for row in keyboards.main_menu().inline_keyboard for b in row]
    check("без WEBAPP_URL кнопок документов нет",
          not any((b.url or "").endswith("/terms") for b in plain))
    config.WEBAPP_URL = "https://example.com"


async def check_platega() -> None:
    print("\nОплата рублями (Platega)")
    config.PLATEGA_MERCHANT = "merchant-123"
    config.PLATEGA_SECRET = "secret-456"

    check("цена пачки берётся из конфига",
          platega.price_of(config.RUB_PACKS[0]["coins"])
          == config.RUB_PACKS[0]["rub"])
    check("чужой пачки нет", platega.price_of(7) is None)

    # Подлинность callback: у Platega нет подписи, она подтверждает себя
    # нашим же секретом в заголовках.
    check("свой секрет проходит", platega.secret_ok(
        {"X-Secret": "secret-456", "X-MerchantId": "merchant-123"}))
    check("чужой секрет не проходит", not platega.secret_ok(
        {"X-Secret": "нет", "X-MerchantId": "merchant-123"}))
    check("чужой мерчант не проходит", not platega.secret_ok(
        {"X-Secret": "secret-456", "X-MerchantId": "нет"}))
    check("пустые заголовки не проходят", not platega.secret_ok({}))

    # Начисление идёт по нашей записи о счёте, а не по телу callback.
    await db.open_invoice("tx-1", "order-1", USER, 100, 200.0)
    before = await db.coins_of(USER)

    # Подделанная сумма в теле не должна ничего изменить.
    result = await platega.apply(
        {"id": "tx-1", "status": "CONFIRMED", "amount": 999999,
         "payload": '{"coins": 999999}'}, None
    )
    check("оплата зачлась", result == "начислено", result)
    after = await db.coins_of(USER)
    check("начислено ровно из нашей записи", after == before + 100,
          f"{before} → {after}")

    # Повтор callback не начисляет второй раз.
    again = await platega.apply({"id": "tx-1", "status": "CONFIRMED"}, None)
    check("повторный callback не зачисляет", again == "уже обработан", again)
    check("баланс не изменился", await db.coins_of(USER) == after)

    # Неизвестный счёт.
    unknown = await platega.apply({"id": "tx-нет", "status": "CONFIRMED"}, None)
    check("неизвестный счёт отбивается", unknown == "счёт неизвестен", unknown)

    # Отмена закрывает счёт и ничего не начисляет.
    await db.open_invoice("tx-2", "order-2", USER, 50, 100.0)
    balance = await db.coins_of(USER)
    canceled = await platega.apply({"id": "tx-2", "status": "CANCELED"}, None)
    check("отмена не начисляет", await db.coins_of(USER) == balance, canceled)
    invoice = await db.invoice("tx-2")
    check("отменённый счёт закрыт", invoice["status"] == "canceled",
          str(invoice["status"]))

    # Висящие счета видны для сверки, закрытые — нет.
    await db.open_invoice("tx-3", "order-3", USER, 50, 100.0)
    pending = await db.pending_invoices()
    ids = {row["transaction_id"] for row in pending}
    check("висящий счёт виден для сверки", "tx-3" in ids, str(ids))
    check("оплаченный в сверку не идёт", "tx-1" not in ids, str(ids))


async def check_variants() -> None:
    print("\nВарианты сообщения")
    account_id = await db.save_account(
        USER, "+79990000400", crypto.encrypt("s"), tg_id=12,
        name="Вариантный", username=None,
    )
    campaign_id = await db.create_campaign(
        USER, account_id, title="Много текстов", text="первый", interval=0,
        source="chats", folder_id=None, chat_ids=[-1001],
        start_at=int(time.time()), pick="order",
    )
    campaign = await db.campaign(USER, campaign_id)

    # Без вариантов берётся то, что лежит в самой рассылке: так работают
    # рассылки, заведённые до появления вариантов.
    fallback = await db.variants(campaign)
    check("без вариантов берётся текст рассылки",
          len(fallback) == 1 and fallback[0]["text"] == "первый",
          str(fallback))

    await db.set_variants(campaign_id, [
        {"content": "text", "text": "первый"},
        {"content": "text", "text": "второй"},
        {"content": "saved", "saved_id": 501},
    ])
    saved = await db.variants(campaign)
    check("варианты записались", len(saved) == 3, str(len(saved)))
    check("порядок сохраняется", saved[0]["text"] == "первый", str(saved[0]))
    check("материал среди вариантов",
          saved[2]["content"] == "saved" and saved[2]["saved_id"] == 501,
          str(saved[2]))

    # По очереди: варианты идут строго по кругу.
    order = []
    for _ in range(5):
        fresh = await db.campaign(USER, campaign_id)
        picked = await broadcast.pick_variant(fresh)
        order.append(picked.get("text") or f"saved:{picked.get('saved_id')}")
    check(f"по очереди: {order}",
          order == ["первый", "второй", "saved:501", "первый", "второй"],
          str(order))

    # Вразнобой: за много попыток встречаются все варианты и порядок не
    # повторяет очередь.
    await db.edit_campaign(
        USER, campaign_id, title="Много текстов", text="первый", interval=0,
        content="text", saved_id=None, pick="random",
    )
    await db.set_variants(campaign_id, [
        {"content": "text", "text": "первый"},
        {"content": "text", "text": "второй"},
        {"content": "text", "text": "третий"},
    ])
    fresh = await db.campaign(USER, campaign_id)
    seen = set()
    for _ in range(60):
        seen.add((await broadcast.pick_variant(fresh))["text"])
    check("вразнобой встречаются все варианты", len(seen) == 3, str(seen))

    # Единственный вариант отдаётся без всякой ротации.
    await db.set_variants(campaign_id, [{"content": "text", "text": "один"}])
    fresh = await db.campaign(USER, campaign_id)
    single = {(await broadcast.pick_variant(fresh))["text"] for _ in range(5)}
    check("один вариант — он и уходит", single == {"один"}, str(single))

    # Удаление рассылки уносит и варианты.
    await db.delete_campaign(USER, campaign_id)
    rest = await db._fetchall(
        "SELECT 1 FROM campaign_texts WHERE campaign_id = ?", (campaign_id,)
    )
    check("варианты удалились вместе с рассылкой", not rest, str(len(rest)))
    await db.delete_account(USER, account_id)


async def check_joins() -> None:
    print("\nПодписка на чаты с ОП")
    account_id = await db.save_account(
        USER, "+79990000500", crypto.encrypt("s"), tg_id=13,
        name="Подписчик", username=None,
    )
    check("вступлений пока нет", await db.joins_today(account_id) == 0)
    check("в чат ещё не ломились",
          not await db.join_tried(account_id, -1001, 86400))

    await db.log_join(account_id, -1001, False, "ChannelPrivateError")
    check("попытка записалась", await db.joins_today(account_id) == 1)
    check("повтор в тот же чат отсекается",
          await db.join_tried(account_id, -1001, 86400))
    check("другой чат не задет",
          not await db.join_tried(account_id, -1002, 86400))

    # Старая попытка не должна держать чат вечно: через сутки в него
    # можно попробовать снова.
    await db._conn().execute(
        "UPDATE joins SET created_at = ? WHERE account_id = ? AND chat_id = ?",
        (int(time.time()) - 90000, account_id, -1001),
    )
    await db._conn().commit()
    check("вчерашняя попытка не блокирует",
          not await db.join_tried(account_id, -1001, 86400))
    check("и в счётчик суток не идёт", await db.joins_today(account_id) == 0)

    # Ошибки, за которыми может стоять обязательная подписка.
    for name in ("ChatWriteForbiddenError", "ChannelPrivateError",
                 "UserNotParticipantError"):
        check(f"{name} ведёт к попытке подписки",
              name in broadcast.JOINABLE)
    check("бан в чате подпиской не лечится",
          "UserBannedInChannelError" not in broadcast.JOINABLE)

    # Потолок вступлений в сутки.
    for chat_id in range(-2000, -2000 + config.JOIN_LIMIT):
        await db.log_join(account_id, chat_id, True, None)
    check("дневной потолок вступлений считается",
          await db.joins_today(account_id) >= config.JOIN_LIMIT,
          str(await db.joins_today(account_id)))

    await db.delete_account(USER, account_id)


async def check_footer_on_media() -> None:
    print("\nПодпись бесплатного тарифа")
    config.FREE_FOOTER = "Рассылка бесплатно — @тестбот"
    # У сообщения с медиа подпись дописывается в конец: смещения
    # оформления считаются от начала подписи, и вставка спереди сдвинула
    # бы жирный текст и премиум-эмодзи.
    text = await broadcast.compose(USER, "текст")
    check("подпись в конце", text.endswith(config.FREE_FOOTER), text)
    check("текст не сдвинут", text.startswith("текст"), text)
    # Пустая подпись у медиа без текста не должна начинаться с пустых строк.
    empty = await broadcast.compose(USER, "")
    check("у пустого текста нет лишних переносов",
          empty == config.FREE_FOOTER, repr(empty))


def check_texts() -> None:
    print("\nТексты и кнопки")
    trial = db.Subscription(kind="trial", until=int(time.time()) + 5 * 86400)
    start = texts.start("Ваня", trial)
    check("в /start есть про пробный период", "бесплатно" in start)
    check("в /start есть про подпись в сообщениях", "подпись" in start.lower()
          or "строка" in start.lower())
    check("склонение дней", texts.plural(1, "день", "дня", "дней") == "день")
    check("склонение дней (2)", texts.plural(2, "день", "дня", "дней") == "дня")
    check("склонение дней (5)", texts.plural(5, "день", "дня", "дней") == "дней")
    check("склонение дней (11)", texts.plural(11, "день", "дня", "дней") == "дней")

    # Без https-адреса кнопки приложения быть не должно: она бы молча не
    # работала, а это хуже, чем её отсутствие.
    config.WEBAPP_URL = ""
    empty = keyboards.main_menu()
    labels = [b.text for row in empty.inline_keyboard for b in row]
    check("без WEBAPP_URL кнопки приложения нет",
          not any("приложение" in text.lower() for text in labels), str(labels))

    config.WEBAPP_URL = "http://localhost:8080"
    plain = keyboards.main_menu()
    labels = [b.text for row in plain.inline_keyboard for b in row]
    check("по http кнопки приложения тоже нет",
          not any("приложение" in text.lower() for text in labels), str(labels))

    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"
    full = keyboards.main_menu()
    buttons = [b for row in full.inline_keyboard for b in row]
    check(
        "кнопка приложения появилась",
        any(b.web_app and b.web_app.url == "https://example.com" for b in buttons),
    )
    check(
        "кнопка поддержки на месте",
        any(b.url == "https://t.me/support" for b in buttons),
    )
    check("кнопка меню — WebApp", type(keyboards.menu_button()).__name__
          == "MenuButtonWebApp")


def check_wiring() -> None:
    print("\nСборка")
    check("роутер бота собран", handlers.router.name == "menu")
    routes = {
        f"{route.method} {route.resource.canonical}"
        for route in webapp.build().router.routes()
    }
    for need in (
        "GET /",
        "POST /api/state",
        "POST /api/login/start",
        "POST /api/login/code",
        "POST /api/login/password",
        "POST /api/account/forget",
    ):
        check(f"есть ручка {need}", need in routes, str(sorted(routes)))


async def run() -> None:
    await db.connect()
    try:
        await check_crypto()
        await check_trial()
        await check_accounts()
        await check_throttle()
        check_phones()
        check_init_data()
        await check_broadcast()
        await check_broadcast_errors()
        await check_broadcast_limits()
        await check_folders()
        await check_tdata()
        await check_account_api()
        await check_payments()
        await check_referrals()
        await check_materials()
        await check_admin()
        await check_legal()
        await check_menu_buttons()
        await check_platega()
        await check_variants()
        await check_joins()
        await check_footer_on_media()
        await check_api()
        check_texts()
        check_wiring()
    finally:
        await db.close()


if __name__ == "__main__":
    print(f"База: {config.DB_PATH}")
    asyncio.run(run())
    print()
    if _failed:
        print(f"Провалено проверок: {_failed}")
        sys.exit(1)
    print("Всё сошлось.")
