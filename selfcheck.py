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
import dataclasses
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
import admin  # noqa: E402
import broadcast  # noqa: E402
import chats  # noqa: E402
import comments  # noqa: E402
import crypto  # noqa: E402
import cryptobot  # noqa: E402
import db  # noqa: E402
import faq  # noqa: E402
import handlers  # noqa: E402
import keyboards  # noqa: E402
import legal  # noqa: E402
import payments  # noqa: E402
import richtext  # noqa: E402
import platega  # noqa: E402
import tdata  # noqa: E402
import texts  # noqa: E402
import tickets  # noqa: E402
import xrocket  # noqa: E402
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


def check_client_args() -> None:
    """Клиент собирается правильно даже после импорта opentele.

    Проверка узкая, но за ней стоит целая поломка на боевом. opentele
    (он разбирает tdata) при импорте подменяет TelegramClient.__init__
    своим и вставляет параметр `api` ВТОРЫМ — между session и api_id.
    Класс остаётся тем же объектом, подмену не видно ничем, а любой
    позиционный вызов после этого разъезжается: api_hash получает число
    вместо строки, и Telethon падает на сериализации словами «bytes or
    str expected, not int». Догадаться по такой ошибке про tdata
    невозможно.

    Хуже всего, что импорт ленивый: пока никто не загружал tdata, вход
    по номеру работает, а после первой же загрузки ломается до
    перезапуска процесса. Такое глазами не ловится — только так.
    """
    print("\nПодключение к Telegram")
    try:
        import opentele.td  # noqa: F401
        import opentele.tl  # noqa: F401
    except Exception as error:  # noqa: BLE001 — без PyQt5 проверять нечего
        print(f"  [ .. ] opentele не поставлен, пропускаем ({error})")
        return

    import inspect

    from telethon import TelegramClient

    names = list(inspect.signature(TelegramClient.__init__).parameters)
    check("opentele действительно вмешивается в конструктор",
          names[:4] == ["self", "session", "api", "api_id"], str(names[:5]))

    config.MTPROTO_API_ID = 12345
    config.MTPROTO_API_HASH = "0123456789abcdef0123456789abcdef"
    client = accounts._client("+79990000001")
    check("api_id доезжает до клиента", client.api_id == 12345,
          repr(client.api_id))
    check("api_hash доезжает строкой, а не числом",
          client.api_hash == config.MTPROTO_API_HASH,
          repr(client.api_hash))

    # То же для подключения к сохранённой сессии: там ключи берутся из
    # аккаунта, но сдвинуться могли бы точно так же.
    params = accounts._api_params(
        db.Account(
            id=1, user_id=USER, phone="+79990000001", tg_id=None, name=None,
            username=None, status="ok", added_at=0, checked_at=0, note=None,
        )
    )
    check("для сохранённой сессии ключи тоже именованные",
          params["api_hash"] == config.MTPROTO_API_HASH
          and isinstance(params["api_id"], int), str(params)[:120])


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
    # Личный диалог в круге остался от старой базы, но сообщение туда не
    # ушло: рассылка в личку — то, за что Telegram блокирует аккаунты
    # быстрее всего, и правило действует даже для заведённых раньше.
    check("в личку не написали", len(FAKE.sent) == 3, str(len(FAKE.sent)))
    check("в журнале сказано почему",
          any("личные сообщения" in (row["error"] or "")
              for row in await db.sends(campaign.id)),
          str([row["error"] for row in await db.sends(campaign.id)]))

    after = await db.campaign(USER, campaign.id)
    check("круг засчитан", after.cycles == 1, str(after.cycles))
    # Три удачных и одна пропущенная личка: круг прошёл целиком.
    check("счётчик удачных сходится", after.sent_ok == 3, str(after.sent_ok))
    check("пропущенная личка попала в ошибки",
          after.sent_err == 1, str(after.sent_err))

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


async def check_xrocket() -> None:
    print("\nОплата через xRocket")

    # Без ключа способ просто не показывается: пустая кнопка оплаты хуже
    # отсутствующей.
    config.XROCKET_TOKEN = ""
    check("без ключа xRocket выключен", not xrocket.ready())

    config.XROCKET_TOKEN = "test-rocket-key"
    check("с ключом включается", xrocket.ready())

    # Цена одна и та же в обоих кошельках: разные цены за одно и то же
    # выглядели бы наценкой за выбор кошелька.
    pack = config.CRYPTO_PACKS[0]
    check("пачки те же, что у CryptoBot",
          xrocket.price_of(pack["coins"]) == pack["usd"],
          str(pack))
    check("чужая пачка не продаётся", xrocket.price_of(999999) is None)

    # Счета обоих кошельков лежат в одной таблице, и сверка каждого
    # обязана видеть только свои: иначе один кошелёк закрывал бы счета
    # другого.
    await db.open_invoice("xrocket:777", "order-x", USER, 50, 1.0,
                          provider="xrocket")
    await db.open_invoice("crypto:888", "order-c", USER, 50, 1.0,
                          provider="crypto")
    pending = await db.pending_invoices()
    mine = [r for r in pending if r["provider"] == "xrocket"]
    check("счёт xRocket отличим от чужого",
          [r["transaction_id"] for r in mine] == ["xrocket:777"], str(pending))

    # Начисление — по нашей записи о счёте, а не по числам из ответа.
    before = await db.coins_of(USER)
    check("оплата зачлась", await xrocket._credit("xrocket:777", None))
    check("монеты начислены", await db.coins_of(USER) == before + 50,
          str(await db.coins_of(USER)))

    # Повтор не должен начислять второй раз: сверка ходит каждые пять
    # минут, и один и тот же счёт она увидит не однажды.
    check("повторная сверка не начисляет дважды",
          not await xrocket._credit("xrocket:777", None))
    check("баланс не тронут", await db.coins_of(USER) == before + 50)

    await db.close_invoice("crypto:888", "expired")
    config.XROCKET_TOKEN = ""


async def check_custom_amount() -> None:
    print("\nСвоё количество монет")

    # Готовая пачка идёт по своей цене даже когда её число ввели руками:
    # в пачке заложена скидка, и пересчёт по базовому курсу отменил бы её
    # тому, кто не нажал на плитку, а набрал то же самое.
    pack = config.RUB_PACKS[-1]
    check("пачка сохраняет свою цену",
          platega.price_for(pack["coins"]) == pack["rub"], str(pack))
    check("и она не совпадает с базовым курсом",
          pack["rub"] < pack["coins"] * config.RUB_PER_COIN, str(pack))

    # Всё остальное — по базовому курсу способа.
    odd = 137
    check("рубли считаются по курсу",
          platega.price_for(odd) == round(odd * config.RUB_PER_COIN, 2),
          str(platega.price_for(odd)))
    check("доллары считаются по курсу",
          cryptobot.price_for(odd) == round(odd / config.COINS_PER_USD, 2),
          str(cryptobot.price_for(odd)))
    check("оба кошелька считают одинаково",
          xrocket.price_for(odd) == cryptobot.price_for(odd))

    # Нижний порог у каждого способа свой: у эквайринга своя минимальная
    # сумма, у криптокошелька — своя. Общий порог обманул бы человека:
    # он ввёл бы «допустимое», а счёт не открылся.
    crypto_min = cryptobot.min_coins()
    check("крипта не продаётся центами",
          crypto_min >= config.CRYPTO_MIN_USD * config.COINS_PER_USD,
          str(crypto_min))
    check("ниже порога крипта не продаётся",
          cryptobot.price_for(crypto_min - 1) is None)
    check("ровно на пороге — продаётся",
          cryptobot.price_for(crypto_min) is not None)
    check("у рублей порог свой",
          platega.min_coins() * config.RUB_PER_COIN >= config.RUB_MIN,
          str(platega.min_coins()))

    # Потолок общий: он про то, сколько монет вообще бывает на счету.
    check("выше потолка не продаётся",
          platega.price_for(config.COIN_MAX + 1) is None
          and cryptobot.price_for(config.COIN_MAX + 1) is None
          and xrocket.price_for(config.COIN_MAX + 1) is None)
    check("мусор вместо числа не продаётся",
          platega.price_for(0) is None and cryptobot.price_for(-5) is None)


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


async def check_custom_menu() -> None:
    print("\nОформление бота из /admin")
    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"

    # Настройки живут в базе и стираются установкой None — так «сбросить»
    # не требует отдельной таблицы флагов.
    await db.set_setting("app_banner_title", "Скидка 20%")
    check("настройка пишется",
          await db.setting("app_banner_title") == "Скидка 20%")
    await db.set_setting("app_banner_title", None)
    check("и стирается", await db.setting("app_banner_title") == "")
    check("значение по умолчанию отдаётся",
          await db.setting("нет-такого", "по умолчанию") == "по умолчанию")

    # Свои кнопки.
    custom = [
        {"text": "Наш канал", "url": "https://t.me/channel"},
        {"text": "Открыть", "action": "app"},
        {"text": "Цены", "action": "tariffs"},
        {"text": "Мусор", "url": "javascript:alert(1)"},
        {"text": "", "url": "https://t.me/empty"},
    ]
    buttons = [b for row in keyboards.main_menu(custom).inline_keyboard for b in row]
    labels = [b.text for b in buttons]

    check("своя кнопка со ссылкой есть", "Наш канал" in labels, str(labels))
    check("своя кнопка приложения есть", "Открыть" in labels, str(labels))
    check("своя кнопка тарифов есть", "Цены" in labels, str(labels))

    # Ссылка не из белого списка схем не должна попасть в меню: её текст
    # пишет владелец, но javascript: в кнопке — это не «своя кнопка».
    check("мусорная схема отбрасывается", "Мусор" not in labels, str(labels))
    check("кнопка без подписи отбрасывается", "" not in labels, str(labels))

    # Кнопка документов остаётся даже поверх своих: её требует банк, и
    # убрать её владелец не может.
    check("документы остаются при своих кнопках",
          any(b.callback_data == "m:docs" for b in buttons), str(labels))

    # Без своих кнопок — обычное меню.
    plain = [b.text for row in keyboards.main_menu().inline_keyboard for b in row]
    check("без настройки меню обычное",
          any("Открыть приложение" in t for t in plain), str(plain))

    # Разбор строк «Текст | куда» — то, что вводит владелец.
    await db.set_setting("menu_buttons", json.dumps(custom, ensure_ascii=False))
    loaded = await handlers.custom_buttons()
    check("кнопки читаются из базы", loaded and len(loaded) == 5, str(loaded))

    # Испорченный JSON не должен ронять /start — только вернуть обычное.
    await db.set_setting("menu_buttons", "{не json")
    check("битые кнопки не ломают меню",
          await handlers.custom_buttons() is None)
    await db.set_setting("menu_buttons", None)
    check("после сброса кнопки обычные",
          await handlers.custom_buttons() is None)

    # Ключи сброса перечислены полностью: забытый ключ пережил бы
    # «сбросить оформление» и остался бы у людей на экране.
    for key in ("menu_chat_id", "menu_msg_id", "menu_buttons",
                "app_banner_title", "app_banner_text"):
        check(f"ключ {key} есть в списке сброса", key in admin.KEYS)


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

        # Путь в поддержку на страницах есть, а личных контактов нет:
        # обращения идут тикетами, у них номер и переписка. Почта в
        # настройках осталась и на страницу не выводится — вернуть её
        # строкой, если однажды потребует касса.
        check(f"{path} с путём в поддержку",
              "поддержк" in html.lower() or path == "/tariffs", path)
        check(f"{path} без личной почты", "support@example.com" not in html, path)
        check(f"{path} без ссылки на чей-то профиль",
              "t.me/support" not in html, path)

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


async def check_tariffs_message() -> None:
    print("\nТарифы сообщением в боте")
    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"
    config.PLATEGA_MERCHANT = "m"
    config.PLATEGA_SECRET = "s"
    config.CRYPTO_TOKEN = "c"

    message = texts.tariffs()

    # Кнопка «Тарифы» должна открывать сообщение, а не уводить на сайт:
    # цены смотрят перед оплатой, и уходить за ними из Telegram незачем.
    buttons = [b for row in keyboards.docs_menu().inline_keyboard for b in row]
    tariff_buttons = [b for b in buttons if "Тариф" in b.text]
    check("кнопка тарифов есть", len(tariff_buttons) == 1, str(len(tariff_buttons)))
    check("она открывает сообщение, а не ссылку",
          tariff_buttons[0].callback_data == "m:tariffs"
          and not tariff_buttons[0].url,
          str(tariff_buttons[0]))

    # Страница при этом никуда не делась: банку нужен адрес, который
    # открывается без Telegram.
    urls = {b.url for b in buttons if b.url}
    check("страница поддержки рядом",
          "https://example.com/support" in urls, str(sorted(urls)))
    check("ссылка на страницу есть в самом сообщении",
          "/tariffs" in message)

    # Цены в сообщении и на странице — из одного источника.
    page = legal.tariffs()
    for plan in config.PLANS:
        piece = f"{plan['stars']} {config.COIN_NAME}"
        check(f"тариф {plan['days']} дн. в сообщении", piece in message, piece)
        check(f"тариф {plan['days']} дн. на странице", piece in page, piece)
    for pack in config.COIN_PACKS:
        check(f"пачка {pack['coins']} за звёзды в сообщении",
              f"{pack['coins']} за {pack['stars']} ⭐️" in message, str(pack))
    for pack in config.RUB_PACKS:
        check(f"пачка {pack['coins']} за рубли в сообщении",
              f"{pack['coins']} за {pack['rub']:.0f} ₽" in message, str(pack))
    for pack in config.CRYPTO_PACKS:
        check(f"пачка {pack['coins']} за доллары в сообщении",
              f"{pack['coins']} за ${pack['usd']:.0f}" in message, str(pack))

    check("лимиты в сообщении",
          str(config.DAILY_LIMIT) in message and str(config.MIN_INTERVAL) in message)
    check("бесплатный период в сообщении", str(config.TRIAL_DAYS) in message)
    check("кодовое слово в сообщении", "mellivora" in message)

    # Способы, которые не подключены, в сообщении не обещаются.
    config.PLATEGA_MERCHANT = ""
    config.PLATEGA_SECRET = ""
    config.CRYPTO_TOKEN = ""
    lean = texts.tariffs()
    check("без Platega рубли не обещаются", "₽" not in lean)
    check("без CryptoBot доллары не обещаются", "$" not in lean)
    check("звёзды остаются всегда", "⭐️" in lean)


async def check_menu_buttons() -> None:
    print("\nМеню бота и документы")
    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"

    top = [b for row in keyboards.main_menu().inline_keyboard for b in row]
    labels = [b.text for b in top]

    # В главном меню документы свёрнуты в одну кнопку: пять служебных
    # кнопок подряд топили «Открыть приложение».
    check("меню короткое", len(top) <= 3, str(labels))
    check("приложение в меню", any(b.web_app for b in top), str(labels))
    # Поддержка ведёт в приложение, а не на чей-то профиль: там
    # обращение превращается в тикет с номером и перепиской, и не
    # теряется в личке среди уведомлений.
    support = [b for b in top if b.text == "Поддержка"]
    check("поддержка в меню", len(support) == 1, str(labels))
    check("поддержка открывает приложение, а не ссылку",
          bool(support and support[0].web_app and not support[0].url),
          str(support))
    check("на чей-то профиль меню не ведёт",
          not any((b.url or "").startswith("https://t.me/") for b in top),
          str([b.url for b in top]))
    check("кнопка документов в меню",
          any(b.callback_data == "m:docs" for b in top), str(labels))
    check("поддержка и документы в одном ряду",
          len(keyboards.main_menu().inline_keyboard[-1]) == 2,
          str([[b.text for b in r] for r in keyboards.main_menu().inline_keyboard]))

    # Всё, что требовал банк, лежит под этой кнопкой — в один тап.
    docs = [b for row in keyboards.docs_menu().inline_keyboard for b in row]
    urls = {b.url for b in docs if b.url}
    for path in ("/terms", "/privacy", "/support"):
        check(f"под кнопкой есть {path}",
              f"https://example.com{path}" in urls, str(sorted(urls)))
    check("под кнопкой есть тарифы",
          any(b.callback_data == "m:tariffs" for b in docs))
    check("под кнопкой есть справка",
          any(b.callback_data == "m:help" for b in docs))
    check("и возврат в меню",
          any(b.callback_data == "m:home" for b in docs))

    # Без адреса приложения ссылок на документы быть не может — вести
    # некуда, и битая кнопка хуже отсутствующей.
    config.WEBAPP_URL = ""
    plain = [b for row in keyboards.docs_menu().inline_keyboard for b in row]
    check("без WEBAPP_URL ссылок на документы нет",
          not any((b.url or "").endswith("/terms") for b in plain))
    check("но тарифы остаются",
          any(b.callback_data == "m:tariffs" for b in plain))
    config.WEBAPP_URL = "https://example.com"


async def check_variables() -> None:
    print("\nПеременные в тексте и оформление")

    # Главное здесь — сдвиг сущностей. Разметку Telegram задаёт не
    # тегами, а координатами «с такого символа, столько символов», и
    # подстановка имени сдвигает всё, что правее. Ошибка в сдвиге видна
    # не как поломка, а как жирный кусок соседнего слова.
    text = "Привет, {name}! Баланс: {balance}"
    entities = [
        {"type": "bold", "offset": 0, "length": 6},          # «Привет»
        {"type": "italic", "offset": 16, "length": 7},       # «Баланс:»
    ]
    out, ents = richtext.apply(text, entities, {"name": "Аня", "balance": 40})
    check("переменные подставились", out == "Привет, Аня! Баланс: 40", out)
    check("сущность слева не сдвинулась",
          ents[0]["offset"] == 0 and ents[0]["length"] == 6, str(ents[0]))
    check("сущность справа поехала на разницу длин",
          out[ents[1]["offset"]:ents[1]["offset"] + ents[1]["length"]] == "Баланс:",
          str(ents[1]) + " → " + repr(out))

    # Эмодзи в UTF-16 занимает две позиции, а в Python — одну. Считать
    # смещения обычным len() значит разъехаться ровно на эту разницу.
    text = "🔥 {name} — молодец"
    entities = [{"type": "bold", "offset": 12, "length": 7}]  # «молодец»
    out, ents = richtext.apply(text, entities, {"name": "Ян"})
    piece = out.encode("utf-16-le")[ents[0]["offset"] * 2:
                                    (ents[0]["offset"] + ents[0]["length"]) * 2]
    check("смещения считаются в UTF-16, а не в символах",
          piece.decode("utf-16-le") == "молодец", repr(out))

    # Премиум-эмодзи — такая же сущность, и она обязана пережить
    # подстановку: ради неё половина этой затеи.
    text = "{name}, привет"
    entities = [{"type": "custom_emoji", "offset": 0, "length": 6,
                 "custom_emoji_id": "5870994129244131212"}]
    out, ents = richtext.apply(text, entities, {"name": "Кот"})
    check("премиум-эмодзи переживает подстановку",
          ents and ents[0]["custom_emoji_id"] == "5870994129244131212",
          str(ents))
    check("и растягивается вместе с текстом внутри",
          ents[0]["length"] == 3, str(ents))

    # Пустая подстановка (человек без ника) не должна оставлять сущность
    # нулевой длины: Telegram такую не примет и отобьёт всё сообщение.
    text = "{username} тут"
    entities = [{"type": "bold", "offset": 0, "length": 10}]
    out, ents = richtext.apply(text, entities, {"username": ""})
    check("сущность нулевой длины выброшена", ents == [], str(ents))

    # Незнакомое в скобках не трогаем — иначе опечатка молча съела бы
    # кусок текста.
    out, _ = richtext.apply("{нетакой} {name}", [], {"name": "Ян"})
    check("неизвестная переменная остаётся как есть",
          out == "{нетакой} Ян", out)

    # Запасной путь: если Telegram откажет в премиум-эмодзи, сообщение
    # уходит без значков, а не пропадает.
    mixed = [{"type": "bold", "offset": 0, "length": 3},
             {"type": "custom_emoji", "offset": 4, "length": 2}]
    check("премиум-эмодзи опознаётся", richtext.has_custom_emoji(mixed))
    left = richtext.drop_custom_emoji(mixed)
    check("без него остальное оформление цело",
          len(left) == 1 and left[0]["type"] == "bold", str(left))

    # Сохранение и чтение из базы.
    check("битые сущности из базы не роняют бота", richtext.load("{не json") == [])
    check("сущности переживают JSON",
          richtext.load(richtext.dump(mixed))[1]["type"] == "custom_emoji")

    # Приветствие с переменными подменяет собой копию: два приветствия
    # одновременно жить не могут, и текст должен быть главнее.
    await db.ensure_user(USER, "test", "Тест Тестов")
    values = await richtext.values_for(
        richtext.person({"user_id": USER, "username": "test", "name": "Тест Тестов"}),
        db,
    )
    check("имя берётся из базы", values["name"] == "Тест", str(values))
    check("ник приходит с собакой", values["username"] == "@test", str(values))
    check("баланс подставляется числом", isinstance(values["balance"], int))
    check("состояние подписки словами", bool(values["tariff"]), str(values))

    for key in ("menu_text", "menu_entities",
                "menu_caption", "menu_caption_entities"):
        check(f"ключ {key} есть в списке сброса", key in admin.KEYS)

    # И самое важное — что /start вообще пойдёт по этой ветке. Настройка,
    # которая сохраняется, но не показывается, выглядит как работающая.
    class FakeBot:
        def __init__(self):
            self.sent = None
            self.copied = None

        async def send_message(self, chat_id, text, **kwargs):
            self.sent = (text, kwargs)

        async def copy_message(self, **kwargs):
            self.copied = kwargs

    await db.set_setting("menu_text", "Привет, {name}! Монет: {balance}")
    await db.set_setting("menu_entities", json.dumps(
        [{"type": "bold", "offset": 0, "length": 6}]))
    bot = FakeBot()
    await handlers.send_start(
        bot, USER,
        richtext.person({"user_id": USER, "username": "test", "name": "Тест"}),
    )
    check("/start показывает текст с переменными", bot.sent is not None)
    check("имя подставилось в приветствие",
          bot.sent and "Привет, Тест!" in bot.sent[0], str(bot.sent))
    check("оформление доехало сущностями",
          bot.sent and bot.sent[1].get("entities"), str(bot.sent))
    check("общий parse_mode заглушён",
          bot.sent and bot.sent[1].get("parse_mode") is None, str(bot.sent))
    check("копирования при этом не было", bot.copied is None)

    # Копия-приветствие не должна перебивать текст, если владелец задал
    # оба: send_start смотрит текст первым — иначе выбор в /admin ничего
    # не решал бы.
    await db.set_setting("menu_chat_id", "1")
    await db.set_setting("menu_msg_id", "2")
    bot = FakeBot()
    await handlers.send_start(bot, USER, None)
    check("текст главнее старой копии",
          bot.sent is not None and bot.copied is None, str(bot.copied))

    await db.set_setting("menu_text", None)
    bot = FakeBot()
    await handlers.send_start(bot, USER, None)
    check("без текста возвращается копия", bot.copied is not None)

    # Медиа с подписью — то, ради чего подпись вообще подменяется:
    # картинка уходит копией, а имя в подписи у каждого своё.
    await db.set_setting("menu_caption", "Привет, {name}! Монет: {balance}")
    await db.set_setting("menu_caption_entities", json.dumps(
        [{"type": "bold", "offset": 0, "length": 6},
         {"type": "italic", "offset": 16, "length": 5}]))
    bot = FakeBot()
    await handlers.send_start(
        bot, USER,
        richtext.person({"user_id": USER, "username": "test", "name": "Тест"}),
    )
    copied = bot.copied or {}
    check("медиа всё ещё копируется, а не пересобирается", bool(copied))
    check("имя подставилось в подпись к медиа",
          "Привет, Тест!" in (copied.get("caption") or ""), str(copied))
    check("разметка подписи доехала сущностями",
          bool(copied.get("caption_entities")), str(copied))
    shifted = copied["caption_entities"][1]
    check("сущность в подписи поехала вместе с текстом",
          copied["caption"][shifted.offset:shifted.offset + shifted.length]
          == "Монет", str(shifted))
    check("общий parse_mode заглушён и здесь",
          copied.get("parse_mode") is None, str(copied))

    # У стикера и кружка подписи нет — подменять нечего, и подставлять
    # пустую строку вместо неё нельзя: Telegram сотрёт настоящую.
    await db.set_setting("menu_caption", None)
    await db.set_setting("menu_caption_entities", None)
    bot = FakeBot()
    await handlers.send_start(bot, USER, None)
    check("без подписи копия уходит нетронутой",
          (bot.copied or {}).get("caption") is None, str(bot.copied))

    for key in ("menu_chat_id", "menu_msg_id", "menu_entities",
                "menu_caption", "menu_caption_entities"):
        await db.set_setting(key, None)


async def check_cast_buttons() -> None:
    print("\nКнопки с эмодзи и цветом, рассылка по людям")
    config.WEBAPP_URL = "https://example.com"
    config.SUPPORT_URL = "https://t.me/support"

    raw = (
        "Наш канал | https://t.me/channel\n"
        "Открыть | app | 5870994129244131212 | голубая\n"
        "Цены | tariffs | | зелёная\n"
        "Мусор | javascript:alert(1)\n"
        "Без адреса |\n"
        "Ерунда | https://t.me/x | синий-в-крапинку"
    )
    items, bad = keyboards.parse_buttons(raw)
    by_text = {item["text"]: item for item in items}

    check("ссылка разобралась", "Наш канал" in by_text, str(by_text))
    check("короткое слово разобралось",
          by_text.get("Открыть", {}).get("action") == "app", str(by_text))
    check("премиум-эмодзи попал в кнопку",
          by_text.get("Открыть", {}).get("emoji") == "5870994129244131212",
          str(by_text))
    check("цвет по-русски понят",
          by_text.get("Открыть", {}).get("style") == "primary", str(by_text))
    check("пустое поле эмодзи пропускается",
          by_text.get("Цены", {}).get("style") == "success"
          and "emoji" not in by_text.get("Цены", {}), str(by_text))

    # Непонятое не проглатываем молча: владелец будет думать, что кнопка
    # есть, а её нет.
    check("мусорная схема не кнопка", "Мусор" not in by_text, str(by_text))
    check("строка без адреса не кнопка", "Без адреса" not in by_text, str(by_text))
    check("непонятный цвет не кнопка", "Ерунда" not in by_text, str(by_text))
    check("о непонятых строках сказано", len(bad) == 3, str(bad))

    # Серая — это отсутствие цвета, а не слово "серая" в поле style.
    grey, _ = keyboards.parse_buttons("Док | tariffs | | серая")
    check("серая кнопка идёт без стиля", "style" not in grey[0], str(grey))

    # Клавиатура под сообщением рассылки.
    markup = keyboards.cast_markup(items)
    flat = [b for row in markup.inline_keyboard for b in row]
    check("кнопки собрались в клавиатуру", len(flat) == 3, str(len(flat)))
    check("по одной кнопке в ряд",
          all(len(row) == 1 for row in markup.inline_keyboard))
    app_button = next(b for b in flat if b.web_app)
    check("эмодзи доехал до кнопки",
          app_button.icon_custom_emoji_id == "5870994129244131212", str(app_button))
    check("цвет доехал до кнопки", app_button.style == "primary", str(app_button))
    check("без кнопок клавиатуры нет", keyboards.cast_markup([]) is None)
    check("из одних мусорных строк клавиатуры тоже нет",
          keyboards.cast_markup([{"text": "x", "url": "javascript:1"}]) is None)

    # Оформление работает и в главном меню, а не только в рассылке.
    menu = [b for row in keyboards.main_menu(items).inline_keyboard for b in row]
    styled = next(b for b in menu if b.text == "Открыть")
    check("своя кнопка меню тоже с эмодзи и цветом",
          styled.icon_custom_emoji_id == "5870994129244131212"
          and styled.style == "primary", str(styled))

    # Получатели рассылки: все, кто заходил, свежие первыми.
    await db.ensure_user(USER, "test", "Тест Тестов")
    await db.ensure_user(OTHER, None, None)
    people = await db.all_recipients()
    ids = [row["user_id"] for row in people]
    check("получатели — все заведённые",
          USER in ids and OTHER in ids, str(ids))
    check("имя и ник приходят вместе с id",
          all("username" in row and "name" in row for row in people), str(people))


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


async def check_attached_media() -> None:
    print("\nМедиа, прикреплённое к тексту")

    # Разбор запроса. Текст и медиа в одном варианте — это подпись к
    # медиа, а не два сообщения подряд.
    known = {501: "photo", 502: "text", 503: "sticker", 504: "gif",
             505: "round", 506: "audio"}

    items, error = webapp._variants({"variants": [
        {"content": "saved", "saved_id": 501, "text": "Привет!"},
    ]}, known)
    check("текст доезжает вместе с медиа", not error and len(items) == 1, error)
    check("и остаётся подписью, а не отдельным вариантом",
          items[0] == {"content": "saved", "text": "Привет!", "saved_id": 501},
          str(items))

    # Голосовое и гифка подпись принимают — их и просили прикреплять.
    for msg_id, what in ((504, "гифке"), (506, "голосовому")):
        items, error = webapp._variants({"variants": [
            {"content": "saved", "saved_id": msg_id, "text": "Слушайте"},
        ]}, known)
        check(f"подпись к {what} принимается", not error and len(items) == 1,
              error)

    # А стикеру и кружку Telegram подписи не даёт, и узнать об этом надо
    # здесь, а не отказом Telegram посреди рассылки.
    for msg_id, what in ((503, "стикеру"), (505, "кружку")):
        items, error = webapp._variants({"variants": [
            {"content": "saved", "saved_id": msg_id, "text": "Подпись"},
        ]}, known)
        check(f"подпись к {what} не принимается", bool(error), str(items))

    # Без текста они по-прежнему прекрасно уходят сами по себе.
    items, error = webapp._variants({"variants": [
        {"content": "saved", "saved_id": 503, "text": ""},
    ]}, known)
    check("стикер без подписи уходит", not error and len(items) == 1, error)

    # Чужой номер сообщения из браузера не принимается: материалы
    # сверяются с кэшем аккаунта.
    items, error = webapp._variants({"variants": [
        {"content": "saved", "saved_id": 999, "text": "чужое"},
    ]}, known)
    check("чужой материал отбрасывается", bool(error) or not items, str(items))

    # Длинный текст отбивается и у варианта с медиа.
    items, error = webapp._variants({"variants": [
        {"content": "saved", "saved_id": 501, "text": "я" * (config.MAX_TEXT + 1)},
    ]}, known)
    check("слишком длинная подпись не проходит", bool(error), str(items))

    # Хранение: текст и медиа живут в одной строке вариантов.
    account_id = await db.save_account(
        USER, "+79990000401", crypto.encrypt("s"), tg_id=13,
        name="С медиа", username=None,
    )
    campaign_id = await db.create_campaign(
        USER, account_id, title="С картинкой", text="", interval=0,
        source="chats", folder_id=None, chat_ids=[-1001],
        start_at=int(time.time()),
    )
    await db.set_variants(campaign_id, [
        {"content": "saved", "saved_id": 501, "text": "Подпись к фото"},
    ])
    stored = await db.variants(await db.campaign(USER, campaign_id))
    check("подпись пережила запись в базу",
          stored[0]["saved_id"] == 501
          and stored[0]["text"] == "Подпись к фото", str(stored))

    await db.delete_campaign(USER, campaign_id)
    await db.delete_account(USER, account_id)

    # Правка не должна терять подпись: до этой ветки она раскладывала
    # варианты на «тексты» и «материалы», и вариант с тем и другим
    # оставался одной картинкой.
    js = (Path(__file__).parent / "webapp" / "app.js").read_text(encoding="utf-8")
    check("правка разбирает варианты с оглядкой на подпись",
          "function splitVariants(" in js and "if (item.content === 'saved' && !text)" in js)
    check("сборка вариантов общая для создания и правки",
          js.count("function packVariants(") == 1
          and "return packVariants(editing);" in js
          and "return packVariants(draft);" in js)


async def check_no_private() -> None:
    print("\nЛичные сообщения")

    # Личный диалог не должен попасть в базу вовсе. Проверяем на самом
    # разборе диалогов: интерфейс можно обойти, а сканирование — нет.
    class Entity:
        def __init__(self, kind, id_):
            self.id = id_
            self.bot = False
            self.access_hash = 1
            self.title = "Чат"
            self.username = None
            self.broadcast = False
            self.megagroup = kind == "group"
            self.first_name = "Человек"
            self.last_name = None
            self._kind = kind

    class Dialog:
        def __init__(self, entity):
            self.entity = entity

    from telethon.tl.types import Channel, User

    person = User(id=777, access_hash=1, first_name="Человек")
    group = Channel(id=888, title="Группа", photo=None, date=None,
                    access_hash=2, megagroup=True)

    class FakeClient:
        def iter_dialogs(self, limit=0):
            async def gen():
                for entity in (person, group):
                    yield Dialog(entity)
            return gen()

    rows = await chats._read_dialogs(FakeClient())
    kinds = {row["kind"] for row in rows}
    check("личный диалог в базу не попадает", "user" not in kinds, str(kinds))
    check("группа попадает", "channel" in kinds or "chat" in kinds, str(kinds))

    # Даже если личка осталась от старого сканирования, выбрать её в
    # рассылку нельзя: ручка сверяет список с чатами аккаунта и
    # отбрасывает личные.
    source = (Path(__file__).parent / "webapp.py").read_text(encoding="utf-8")
    check("ручка отсекает личные чаты",
          'if chat.kind != "user"' in source)

    # И последняя линия — сама отправка.
    engine = (Path(__file__).parent / "broadcast.py").read_text(encoding="utf-8")
    check("отправка отказывается писать в личку",
          "class PrivateChat" in engine
          and 'if chat.kind == "user":' in engine)
    check("и пишет причину в журнал",
          "в личные сообщения рассылка не идёт" in engine)

    # Папка «контакты» больше ничего не разворачивает: разворачивать
    # нечем, личных диалогов в базе нет.
    folders = (Path(__file__).parent / "chats.py").read_text(encoding="utf-8")
    check("папка контактов не разворачивается",
          'chat_ids += by_kind["user"]' not in folders)

    # И людям про это сказано — иначе они будут искать личку в списке.
    check("в приветствии сказано про личку",
          "В личные сообщения бот не пишет" in texts.start(None, await db.subscription(USER)))
    check("в частых вопросах есть объяснение",
          any("личные сообщения" in item["q"] for item in faq.items()),
          str([item["q"] for item in faq.items()]))


async def check_tickets() -> None:
    print("\nТикеты поддержки")
    await db.ensure_user(USER, "test", "Тест Тестов")
    await db.ensure_user(OTHER, None, None)

    first = await db.create_ticket(USER, "Не подключается аккаунт")
    await db.add_ticket_message(first, "user", "Пишет «не удалось запросить код».")

    found = await db.ticket(first, USER)
    check("тикет завёлся", found is not None and found["status"] == "open",
          str(found))
    check("чужой тикет не читается", await db.ticket(first, OTHER) is None)

    # Статус ведёт переписка: ответил владелец — тикет больше не ждёт.
    await db.add_ticket_message(first, "admin", "Проверьте ключи в .env.")
    check("после ответа тикет не ждёт",
          (await db.ticket(first))["status"] == "answered")
    await db.add_ticket_message(first, "user", "Ключи те же.")
    check("написал человек — снова ждёт",
          (await db.ticket(first))["status"] == "open")

    messages = await db.ticket_messages(first)
    check("переписка в порядке появления",
          [m["author"] for m in messages] == ["user", "admin", "user"],
          str([m["author"] for m in messages]))

    # Список для владельца: первым то, что ждёт дольше всех.
    second = await db.create_ticket(USER, "Второе обращение")
    await db.add_ticket_message(second, "user", "И ещё вопрос.")
    await db._conn().execute(
        "UPDATE tickets SET updated_at = ? WHERE id = ?",
        (int(time.time()) - 3600, first),
    )
    await db._conn().commit()
    waiting = await db.open_tickets()
    check("в очереди сначала самые старые",
          [row["id"] for row in waiting][:2] == [first, second],
          str([row["id"] for row in waiting]))
    check("в очереди видно, кто написал",
          waiting[0].get("username") == "test", str(waiting[0].get("username")))
    check("счётчик открытых считает", await db.count_open_tickets() >= 2)

    # Закрытый уходит из очереди, но остаётся у человека.
    await db.set_ticket_status(second, "closed")
    waiting = await db.open_tickets()
    check("закрытый из очереди уходит",
          not any(row["id"] == second for row in waiting))
    mine = await db.tickets_of(USER)
    check("но в своём списке остаётся",
          any(row["id"] == second for row in mine), str(len(mine)))

    check("суточный счётчик считает",
          await db.count_tickets_today(USER) == 2,
          str(await db.count_tickets_today(USER)))

    # Тип вложения: у гифки есть и animation, и document, у голосового —
    # и voice, и document. Общий тип должен победить, иначе гифка уедет
    # обратно файлом, а голосовое станет неслушаемым.
    class Fake:
        photo = None
        video = None
        voice = None
        audio = None

        def __init__(self, **kw):
            self.animation = None
            self.document = None
            for key, value in kw.items():
                setattr(self, key, value)

    class Item:
        def __init__(self, file_id):
            self.file_id = file_id
            self.file_name = "f"

    gif = Fake(animation=Item("gif-id"), document=Item("doc-id"))
    check("гифка опознаётся гифкой",
          tickets.file_from(gif)[:2] == ("animation", "gif-id"),
          str(tickets.file_from(gif)))
    voice = Fake(voice=Item("voice-id"), document=Item("doc-id"))
    check("голосовое опознаётся голосовым",
          tickets.file_from(voice)[:2] == ("voice", "voice-id"),
          str(tickets.file_from(voice)))
    plain = Fake()
    check("без вложения — ничего", tickets.file_from(plain) == (None, None, None))

    # Все типы, которые принимаем, бот умеет слать: иначе вложение
    # пропало бы уже у владельца.
    from aiogram import Bot

    for kind, method in tickets.KINDS.items():
        check(f"бот умеет {method} для {kind}", hasattr(Bot, method))


async def check_ban_and_faq() -> None:
    print("\nБлокировка и частые вопросы")

    # Частые вопросы описывают устройство сервиса, поэтому числа в них
    # берутся из настроек. Иначе правишь лимит в config, а в ответе он
    # остаётся прежним — и человек читает неправду.
    items = faq.items()
    check("вопросы есть", len(items) >= 5, str(len(items)))
    check("у каждого есть ответ",
          all(item["q"] and item["a"] for item in items))
    config.DAILY_LIMIT = 137
    check("числа подставляются из настроек",
          any("137" in item["a"] for item in faq.items()),
          str([item["a"][:40] for item in faq.items()])[:120])
    config.DAILY_LIMIT = 200

    # Блокировка.
    victim = 555999
    await db.ensure_user(victim, "spammer", "Спамер")
    check("по умолчанию доступ открыт",
          await db.is_banned(victim) == (False, ""))

    account_id = await db.save_account(
        victim, "+79990000700", crypto.encrypt("s"), tg_id=70,
        name="Спамерский", username=None,
    )
    campaign_id = await db.create_campaign(
        victim, account_id, title="Спам", text="привет", interval=0,
        source="chats", folder_id=None, chat_ids=[-1001],
        start_at=int(time.time()),
    )
    watch_id = await db.create_watch(
        victim, account_id, -2001, "Канал", 0, 0, 0,
    )

    await db.set_banned(victim, True, "спам в обращениях")
    banned, reason = await db.is_banned(victim)
    check("бан ставится", banned and reason == "спам в обращениях", reason)

    stopped = await db.stop_user_work(victim, "доступ закрыт")
    check("рассылка и наблюдение встали", stopped == 2, str(stopped))
    check("рассылка действительно остановлена",
          (await db.campaign(victim, campaign_id)).status == "stopped")
    check("наблюдение тоже",
          (await db.watch(victim, watch_id)).status == "stopped")

    # Бан — про поведение, а не про деньги: купленное остаётся.
    await db.add_coins(victim, 50, "тест")
    check("монеты у забаненного остаются",
          await db.coins_of(victim) == 50, str(await db.coins_of(victim)))

    check("забаненный есть в списке",
          any(row["user_id"] == victim for row in await db.banned_users()))

    await db.set_banned(victim, False, "")
    check("разбан снимает и причину",
          await db.is_banned(victim) == (False, ""))
    check("из списка забаненных исчез",
          not any(row["user_id"] == victim for row in await db.banned_users()))

    # Владельца забанить нельзя — проверка стоит в ручке, а не в базе:
    # база не должна знать про роли.
    source = (Path(__file__).parent / "webapp.py").read_text(encoding="utf-8")
    check("владельца забанить нельзя",
          "Владельца забанить нельзя" in source)
    check("бан закрыт от чужих",
          "async def api_admin_ban" in source
          and source.index("@admin_only\nasync def api_admin_ban")
          < source.index("async def api_admin_ban") + 20)

    # Обращений в сутки — два, и это настройка, а не число в коде.
    check("лимит обращений — настройка",
          config.TICKETS_PER_DAY == 2, str(config.TICKETS_PER_DAY))
    check("лимит виден приложению",
          "tickets_per_day" in source)

    await db.delete_campaign(victim, campaign_id)
    await db.delete_watch(victim, watch_id)
    await db.delete_account(victim, account_id)


async def check_paid_chats() -> None:
    print("\nПлатные чаты и загрузка медиа")
    account_id = await db.save_account(
        USER, "+79990000600", crypto.encrypt("s"), tg_id=60,
        name="Платный", username=None,
    )
    await db.save_chats(account_id, [
        {"chat_id": -3001, "raw_id": 3001, "access_hash": 1, "kind": "channel",
         "broadcast": False, "title": "Бесплатная группа", "username": None},
        {"chat_id": -3002, "raw_id": 3002, "access_hash": 2, "kind": "channel",
         "broadcast": False, "title": "Платная группа", "username": None,
         "paid_stars": 25},
        {"chat_id": -3003, "raw_id": 3003, "access_hash": 3, "kind": "channel",
         "broadcast": False, "title": "Очень платная", "username": None,
         "paid_stars": 500},
    ])
    saved = {c.chat_id: c for c in await db.chats(account_id)}
    check("цена чата сохранилась", saved[-3002].paid_stars == 25,
          str(saved[-3002].paid_stars))
    check("бесплатный так и остался бесплатным",
          saved[-3001].paid_stars == 0)

    # По умолчанию в платные чаты не пишем: звёзды списываются с
    # подключённого аккаунта, и тратить их молча нельзя.
    check("потолок по умолчанию нулевой", await db.paid_max_stars(USER) == 0)
    check("бесплатный чат проходит без вопросов",
          await broadcast.paid_check(USER, saved[-3001]) == 0)
    try:
        await broadcast.paid_check(USER, saved[-3002])
        check("платный чат без разрешения не проходит", False)
    except broadcast.TooExpensive as error:
        check("платный чат без разрешения не проходит", error.stars == 25)

    # Разрешили — пишем, и ровно столько, сколько просит чат.
    await db.set_paid_max_stars(USER, 50)
    check("потолок сохранился", await db.paid_max_stars(USER) == 50)
    check("чат по карману проходит",
          await broadcast.paid_check(USER, saved[-3002]) == 25)
    try:
        await broadcast.paid_check(USER, saved[-3003])
        check("чат дороже потолка не проходит", False)
    except broadcast.TooExpensive as error:
        check("чат дороже потолка не проходит",
              error.stars == 500 and error.limit == 50)

    # Ровно на потолке — проходит: иначе «до 50» читалось бы как «до 49».
    await db.set_paid_max_stars(USER, 25)
    check("ровно на потолке проходит",
          await broadcast.paid_check(USER, saved[-3002]) == 25)
    await db.set_paid_max_stars(USER, 0)

    # Флаг оплаты у Telethon есть только в самих запросах, не в
    # высокоуровневых методах, — на этом построена вся ветка send_paid.
    from telethon.tl.functions.messages import SendMediaRequest, SendMessageRequest
    import inspect

    for request in (SendMessageRequest, SendMediaRequest):
        check(f"{request.__name__} умеет allow_paid_stars",
              "allow_paid_stars" in inspect.signature(request.__init__).parameters)

    # Загрузка медиа в «Избранное» — чтобы не ходить туда руками.
    js = (Path(__file__).parent / "webapp" / "app.js").read_text(encoding="utf-8")
    check("в приложении есть загрузка файла",
          "function uploadMaterial(" in js and "/api/material/upload" in js)
    check("гифка и голосовое уходят без сжатия",
          "gif|ogg|oga|mp3|m4a|pdf" in js)
    check("у загрузки есть свой потолок размера",
          config.MAX_UPLOAD_MB > 0 and "max_upload_mb" in js)

    await db.delete_account(USER, account_id)


async def check_watches() -> None:
    print("\nАвтокомментарии под постами")
    account_id = await db.save_account(
        USER, "+79990000500", crypto.encrypt("s"), tg_id=50,
        name="Комментатор", username=None,
    )
    await db.save_chats(account_id, [
        {"chat_id": -2001, "raw_id": 2001, "access_hash": 11, "kind": "channel",
         "broadcast": True, "title": "Канал с раздачами", "username": None},
    ])

    watch_id = await db.create_watch(
        USER, account_id, -2001, "Канал с раздачами",
        last_msg_id=500, delay_min=30, delay_max=120, pick="order",
    )
    await db.set_watch_variants(watch_id, [
        {"content": "text", "text": "Участвую!"},
        {"content": "saved", "text": "Спасибо", "saved_id": 501},
    ])

    watch = await db.watch(USER, watch_id)
    check("наблюдение завелось", watch is not None and watch.chat_id == -2001)
    check("точка отсчёта — текущий пост",
          watch.last_msg_id == 500, str(watch.last_msg_id))

    items = await db.watch_variants(watch_id)
    check("варианты ответа записались", len(items) == 2, str(items))
    check("подпись к материалу сохранилась",
          items[1]["saved_id"] == 501 and items[1]["text"] == "Спасибо",
          str(items[1]))

    # Отметка двигается и при ошибке: пост, на который ответить не вышло,
    # иначе застрял бы в очереди навсегда и закрыл собой все следующие.
    await db.mark_watch_seen(watch_id, 501, ok=False)
    after = await db.watch(USER, watch_id)
    check("после ошибки отметка сдвинулась", after.last_msg_id == 501)
    check("ошибка посчиталась", after.sent_err == 1, str(after.sent_err))
    await db.mark_watch_seen(watch_id, 502, ok=True)
    after = await db.watch(USER, watch_id)
    check("ответ посчитался", after.sent_ok == 1, str(after.sent_ok))

    # Очередь на выполнение.
    await db.reschedule_watch(watch_id, int(time.time()) - 1)
    due = await db.due_watches(int(time.time()))
    check("наблюдение попадает в очередь",
          any(w.id == watch_id for w in due), str([w.id for w in due]))
    await db.set_watch_status(watch_id, "paused", "руками")
    due = await db.due_watches(int(time.time()))
    check("на паузе очередь его не берёт",
          not any(w.id == watch_id for w in due))
    await db.set_watch_status(watch_id, "running", None)

    # Задержка — всегда внутри вилки и не ниже общей планки.
    fresh = await db.watch(USER, watch_id)
    delays = {comments._delay(fresh) for _ in range(40)}
    check("задержка внутри вилки",
          all(fresh.delay_min <= d <= fresh.delay_max for d in delays),
          str(sorted(delays)[:5]))
    check("задержка не всегда одна и та же", len(delays) > 1, str(delays))

    import dataclasses

    tight = dataclasses.replace(fresh, delay_min=0, delay_max=0)
    check("ниже общей планки задержка не опускается",
          comments._delay(tight) >= config.COMMENT_MIN_DELAY,
          str(comments._delay(tight)))

    # Мгновенный режим — то, ради чего это и просили: в раздачах
    # подарки достаются первым, и любая пауза означает, что их разберут
    # без нас.
    instant = dataclasses.replace(fresh, delay_min=0, delay_max=0)
    config.COMMENT_MIN_DELAY = 0
    check("нулевая вилка означает ответ без паузы",
          comments._delay(instant) == 0, str(comments._delay(instant)))

    # Владелец бота может поднять общую планку, и тогда она сильнее
    # настройки наблюдения: это его решение про все аккаунты сразу.
    config.COMMENT_MIN_DELAY = 20
    check("общая планка сильнее нулевой вилки",
          comments._delay(instant) == 20, str(comments._delay(instant)))
    config.COMMENT_MIN_DELAY = 0

    # Слушатели: движок держит подписку на посты по включённым
    # наблюдениям, и список берётся без оглядки на владельца — слушать
    # надо все, а не только свои.
    running = await db.watches_running()
    check("включённое наблюдение попадает в слушатели",
          any(w.id == watch_id for w in running), str([w.id for w in running]))
    found = await db.watch_by_chat(account_id, -2001)
    check("наблюдение находится по аккаунту и каналу",
          found is not None and found.id == watch_id, str(found))
    check("по чужому каналу не находится",
          await db.watch_by_chat(account_id, -9999) is None)
    check("аккаунт достаётся движку без user_id",
          (await db.account_by_id(account_id)) is not None)

    await db.set_watch_status(watch_id, "paused", None)
    running = await db.watches_running()
    check("выключенное наблюдение из слушателей уходит",
          not any(w.id == watch_id for w in running))
    await db.set_watch_status(watch_id, "running", None)

    # Подключение со слушателем нельзя закрывать по простою: слушатель
    # уехал бы вместе с ним, а наблюдение осталось бы «включённым».
    check("подключение со слушателем помечается", hasattr(broadcast._Live, "keep")
          or "keep" in broadcast._Live.__dataclass_fields__)
    js = (Path(__file__).parent / "broadcast.py").read_text(encoding="utf-8")
    check("и обходится закрытием по простою",
          "if live.keep:\n            continue" in js)

    # Мёртвый аккаунт уносит и наблюдения: писать всё равно нечем.
    stopped = await db.stop_account_watches(account_id, "сессия отозвана")
    check("наблюдения останавливаются вместе с аккаунтом", stopped == 1,
          str(stopped))
    check("и причина записана",
          (await db.watch(USER, watch_id)).note == "сессия отозвана")

    # Удаление уносит и варианты.
    check("наблюдение удаляется", await db.delete_watch(USER, watch_id))
    rest = await db._fetchall(
        "SELECT 1 FROM watch_texts WHERE watch_id = ?", (watch_id,)
    )
    check("варианты ответа удалились вместе с ним", not rest, str(len(rest)))
    check("чужое наблюдение не удалить",
          not await db.delete_watch(OTHER, watch_id))

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
        any(b.text == "Поддержка" and b.web_app for b in buttons),
        str([b.text for b in buttons]),
    )
    check("кнопка меню — WebApp", type(keyboards.menu_button()).__name__
          == "MenuButtonWebApp")


async def check_radar_filter() -> None:
    """Предфильтр: он решает, за что мы платим и что теряем.

    Обе ошибки здесь стоят денег, но по-разному. Лишний пропуск — это
    доли копейки за разбор. Пропущенный заказ — это заказ. Поэтому
    настоящие заказы в списке ниже важнее мусора: если сломается
    отсечение рекламы, вырастет счёт, а если сломается пропуск заказов,
    радар молча перестанет работать, и заметить это будет не по чему.
    """
    import classify

    print("\nРадар: предфильтр")
    passes = (
        "Ищу разработчика телеграм-бота для записи клиентов в барбершоп, "
        "бюджет 30к",
        "Нужен бот для рассылки по базе, есть ТЗ. Пишите в лс с ценой",
        "Кто может допилить бота на aiogram? Отвалилась оплата, срочно",
        "Требуется специалист для интеграции CRM с ботом, оплата по факту",
        "Надо сделать мини-апп для магазина, каталог и корзина. Кто возьмётся",
        "Нужен бот для рассылки, бюджет 20к",
    )
    for text in passes:
        check(f"заказ проходит: {text[:38]}…", classify.prefilter(text), text)

    mirror = (
        "Ищу работу, делаю ботов на python, опыт 3 года, портфолио в лс",
        "Возьму заказ на разработку телеграм-бота, цена договорная",
        "Готов взяться за проект любой сложности, мои работы в закрепе",
        "Делаю ботов под ключ, пишите, обсудим бюджет",
        "Оказываю услуги по разработке ботов, бюджет обсуждается",
        "В поиске работы, рассмотрю предложения по разработке ботов",
    )
    for text in mirror:
        check(
            f"реклама исполнителя отсекается: {text[:30]}…",
            not classify.prefilter(text),
            text,
        )

    noise = (
        "+",
        "да ладно, серьёзно что ли",
        "https://t.me/somechannel",
        "А какой фреймворк лучше для ботов, aiogram или telebot? Кто что "
        "думает, хочу начать изучать",
    )
    for text in noise:
        check(f"шум отсекается: {text[:30]}", not classify.prefilter(text), text)

    check(
        "ключевые слова отсекают чужой домен",
        not classify.prefilter("Ищу дизайнера для логотипа, бюджет 15к",
                               "бот, aiogram"),
    )
    check(
        "ключевые слова пропускают свой домен",
        classify.prefilter("Нужен телеграм бот для доставки, бюджет 40000",
                           "бот, aiogram"),
    )
    check(
        "без ключевых слов решает модель, а не сито",
        classify.prefilter("Ищу дизайнера для логотипа, бюджет 15к", ""),
    )

    # Отпечаток: на нём держится склейка повторов, а повторы — главная
    # причина, по которой лента лидов становится нечитаемой.
    one = "Ищу разработчика бота для записи в барбершоп. Бюджет 30к!"
    two = "ищу разработчика бота для записи в барбершоп   бюджет 30к"
    three = "Ищу разработчика бота для доставки еды. Бюджет 30к"
    check(
        "тот же заказ с другой пунктуацией склеивается",
        classify.fingerprint(1, one) == classify.fingerprint(1, two),
    )
    check(
        "другой заказ того же автора не склеивается",
        classify.fingerprint(1, one) != classify.fingerprint(1, three),
    )
    check(
        "тот же текст от другого автора не склеивается",
        classify.fingerprint(1, one) != classify.fingerprint(2, one),
    )


async def check_radar_verdicts() -> None:
    """Разбор ответа модели.

    Главное здесь — раскладка по номерам. Модель отвечает номерами, а не
    порядком, и один пропущенный номер при наивной раскладке сдвинул бы
    все вердикты на единицу: заказ уехал бы на чужое сообщение, с чужим
    автором и чужим чатом в карточке. Заметить такое по внешнему виду
    ленты почти невозможно.
    """
    import classify

    print("\nРадар: разбор ответа модели")

    class Block:
        type = "text"

        def __init__(self, text: str) -> None:
            self.text = text

    class Reply:
        stop_reason = "end_turn"

        def __init__(self, text: str) -> None:
            self.content = [Block(text)]

    out = classify._unpack(
        Reply('{"verdicts":['
              '{"n":2,"order":true,"score":77,"budget":15000,"currency":"RUB",'
              '"urgency":"normal","stack":["python"],"summary":"Парсер"},'
              '{"n":1,"order":false,"score":5,"budget":0,"currency":"",'
              '"urgency":"low","stack":[],"summary":""}]}'),
        2,
    )
    check("вердиктов столько же, сколько сообщений", len(out) == 2)
    check(
        "вердикты кладутся по номерам, а не по порядку ответа",
        out[0].order is False and out[1].order is True,
    )
    check("поля вердикта читаются", out[1].score == 77 and out[1].budget == 15000)

    out = classify._unpack(
        Reply('{"verdicts":[{"n":1,"order":true,"score":90,"budget":0,'
              '"currency":"","urgency":"low","stack":[],"summary":"a"}]}'),
        3,
    )
    check(
        "пропущенный номер не сдвигает соседей",
        len(out) == 3 and not out[1].order and not out[2].order,
    )

    out = classify._unpack(
        Reply('{"verdicts":[{"n":9,"order":true,"score":90,"budget":0,'
              '"currency":"","urgency":"low","stack":[],"summary":"a"}]}'),
        2,
    )
    check("номер за пределами пачки отбрасывается", not any(v.order for v in out))

    check(
        "мусор вместо JSON не роняет разбор",
        not any(v.order for v in classify._unpack(Reply("не json"), 2)),
    )

    class Refused(Reply):
        stop_reason = "refusal"

    check(
        "отказ модели не роняет разбор",
        not any(v.order for v in classify._unpack(Refused("{}"), 2)),
    )

    one = classify._unpack(
        Reply('{"verdicts":[{"n":1,"order":true,"score":900,"budget":-5,'
              '"currency":"РУБЛЕЙРУБЛЕЙРУБЛЕЙ","urgency":"low",'
              '"stack":["a","b","c","d","e","f","g"],'
              '"summary":"' + "x" * 400 + '"}]}'),
        1,
    )[0]
    check("оценка вне диапазона подрезается", one.score == 100)
    check("отрицательный бюджет обнуляется", one.budget == 0)
    check("длины полей подрезаются", len(one.stack) == 5 and len(one.summary) == 200)

    check(
        "без ключа классификатор честно выключен",
        not classify.ready() if not config.ANTHROPIC_API_KEY else True,
    )


async def check_radar_storage() -> None:
    """Хранилище радара: курсоры и склейка повторов."""
    import classify

    print("\nРадар: хранилище")
    account_id = await db.save_account(
        USER, "+79990000777", crypto.encrypt("session"),
        tg_id=777001, name="Радарный", username=None,
    )
    radar = await db.create_radar(
        USER, account_id, "Боты", "делаю телеграм-ботов на python и aiogram"
    )
    check("радар создан включённым", radar.status == "running")
    check("радар виден воркеру", any(
        r.id == radar.id for r in await db.radars_running()
    ))

    await db.mark_account(account_id, "dead")
    check(
        "радар на мёртвом аккаунте воркеру не выдаётся",
        not any(r.id == radar.id for r in await db.radars_running()),
    )
    await db.mark_account(account_id, "ok")

    added = await db.add_radar_chats(radar.id, [
        {"chat_id": -1001, "title": "Заказы IT"},
        {"chat_id": -1002, "title": "Фриланс боты"},
    ])
    check("чаты добавлены", added == 2, str(added))
    check(
        "повторное добавление чата ничего не создаёт",
        await db.add_radar_chats(radar.id, [{"chat_id": -1001, "title": "x"}]) == 0,
    )

    await db.set_radar_cursor(radar.id, -1001, 500)
    await db.set_radar_cursor(radar.id, -1001, 400)
    rows = {c.chat_id: c for c in await db.radar_chats(radar.id)}
    check("курсор назад не откатывается", rows[-1001].last_msg_id == 500,
          str(rows[-1001].last_msg_id))

    text = "Ищу разработчика бота для записи в барбершоп, бюджет 30к"
    fields = {
        "chat_id": -1001, "chat_title": "Заказы IT", "msg_id": 501,
        "author_id": 777, "author_name": "Иван", "author_user": "ivan",
        "text": text, "score": 82, "budget": 30000, "currency": "RUB",
        "urgency": "high", "stack": ["python", "aiogram"],
        "summary": "Бот записи", "verdict": "order",
    }
    mark = classify.fingerprint(777, text)
    lead, fresh = await db.save_lead(radar.id, USER, mark, fields)
    check("первый заказ записан как новый", fresh)
    check("стек читается обратно списком", lead.stack == ["python", "aiogram"])

    repeat, again = await db.save_lead(
        radar.id, USER, mark,
        dict(fields, chat_id=-1002, chat_title="Фриланс боты", msg_id=88),
    )
    check("повтор в другом чате не создаёт второй лид", not again)
    check("у повтора вырос счётчик", repeat.repeats == 1, str(repeat.repeats))
    check("карточка осталась от первого чата", repeat.chat_title == "Заказы IT")

    other = classify.fingerprint(777, "Нужен бот для доставки еды, бюджет 50к")
    _, third = await db.save_lead(radar.id, USER, other, dict(fields, msg_id=99))
    check("другой заказ того же автора — отдельный лид", third)

    await db.save_lead(radar.id, USER, "fp-skip",
                       dict(fields, msg_id=100, verdict="skip", score=12))
    check("заказы и отсеянное лежат отдельно",
          len(await db.leads(USER, verdict="order")) == 2
          and len(await db.leads(USER, verdict="skip")) == 1)

    await db.bump_radar_usage(USER, 1, 8)
    await db.bump_radar_usage(USER, 2, 5)
    check("расход классификатора накапливается за сутки",
          await db.radar_usage_today(USER) == (3, 13),
          str(await db.radar_usage_today(USER)))

    await db.delete_radar(USER, radar.id)
    check("радар удалён", await db.radar(USER, radar.id) is None)
    check("чаты ушли вместе с радаром", not await db.radar_chats(radar.id))
    check("найденные заказы пережили удаление радара",
          len(await db.leads(USER, verdict="order")) == 2)


def check_radar_card() -> None:
    """Карточка находки. Она уходит в личку человеку, и любая чужая
    угловая скобка в имени автора сломала бы разметку сообщения."""
    import texts

    print("\nРадар: карточка находки")
    lead = db.Lead(
        id=1, radar_id=1, user_id=USER, chat_id=-1001,
        chat_title="Заказы IT & боты", msg_id=501, author_id=777,
        author_name="Иван <Петров>", author_user="ivan",
        text="Ищем разработчика бота для записи клиентов. Бюджет 30к.",
        score=82, budget=30000, currency="RUB", urgency="high",
        stack=["python", "aiogram"], summary="Бот записи в барбершоп",
        verdict="order", judged=True, repeats=2, notified=False, created_at=0,
    )
    card = texts.lead_card(lead, "https://t.me/c/1001/501")
    check("имя автора экранируется", "&lt;Петров&gt;" in card)
    check("название чата экранируется", "&amp;" in card)
    check("бюджет с разделителем разрядов", "30 000 ₽" in card)
    check("повторы считаются вместе с исходным", "повтор в 3 чатах" in card)
    check("ссылка на сообщение на месте", "Открыть сообщение" in card)

    thin = dataclasses.replace(lead, budget=0, currency=None,
                               urgency="normal", stack=[], repeats=0)
    plain = texts.lead_card(thin, None)
    check("пустой бюджет подписан словами", "бюджет не назван" in plain)
    check("обычная срочность не пишется", "🔥" not in plain)
    check("без ссылки строки нет", "Открыть" not in plain)


async def check_radar_offline() -> None:
    """Работа радара без ключа классификатора.

    Это не запасной режим «на всякий случай», а обычное состояние для
    всех, у кого ключа нет и не будет. Ломается он молча и обиднее
    всего: сообщения собираются, счётчики растут, а лента пустая —
    снаружи не отличить от «в чатах просто нет заказов».
    """
    import classify

    print("\nРадар без модели")

    check(
        "без ключа классификатор выключен",
        not classify.ready() if not config.ANTHROPIC_API_KEY else True,
    )

    money = (
        ("бюджет 30к", 30000, "RUB"),
        ("нужен бот, 15 000 руб", 15000, "RUB"),
        ("оплата $500", 500, "USD"),
        ("€1200 за проект", 1200, "EUR"),
        ("от 20 до 50 тысяч", 20000, "RUB"),
        ("бюджет 15-20к", 15000, "RUB"),
        ("срок 14 дней, бюджет 45000", 45000, "RUB"),
        ("нужен парсер, 50 тыс рублей", 50000, "RUB"),
    )
    for text, amount, money_name in money:
        got = classify.budget_of(text)
        check(f"бюджет из «{text}»", got == (amount, money_name), str(got))

    blind = (
        "бот для 1000 пользователей",
        "ищу бота, деталей пока нет",
        "нужен бот, обсудим позже",
    )
    for text in blind:
        check(
            f"без цифр бюджет не выдумывается: «{text[:30]}»",
            classify.budget_of(text) == (0, ""),
            str(classify.budget_of(text)),
        )

    # Главное. Раньше здесь возвращались отказы на всё подряд, и каждая
    # находка молча уезжала в отсеянное.
    items = [
        classify.Candidate(
            -100, "Заказы IT", 1, 7, "Иван", "ivan",
            "Ищу разработчика телеграм-бота для записи в барбершоп. "
            "Бюджет 30к, нужно срочно",
        ),
        classify.Candidate(
            -100, "Заказы IT", 2, 8, "Пётр", None,
            "Нужен парсер сайта в гугл-таблицу, деталей пока не много",
        ),
    ]
    verdicts = await classify.classify("делаю телеграм-ботов", items)
    check("вердиктов столько же, сколько сообщений", len(verdicts) == 2)
    check(
        "прошедшее сито считается заказом, а не отказом",
        all(v.order for v in verdicts),
    )
    check(
        "вердикт помечен как неразобранный",
        all(not v.judged for v in verdicts),
    )
    check("бюджет вытащен регуляркой", verdicts[0].budget == 30000,
          str(verdicts[0].budget))
    check("срочность замечена", verdicts[0].urgency == "high")
    check("суть взята из первой фразы",
          verdicts[0].summary.startswith("Ищу разработчика"),
          verdicts[0].summary)

    # Оценка обязана дотягивать до планки показа: вердикт ниже неё —
    # это та же пустая лента, только окольным путём.
    floor = config.RADAR_MIN_SCORE
    check(
        f"оценка без модели проходит планку показа ({floor})",
        all(v.score >= floor for v in verdicts),
        str([v.score for v in verdicts]),
    )
    check(
        "названные деньги поднимают оценку",
        verdicts[0].score > verdicts[1].score,
        f"{verdicts[0].score} против {verdicts[1].score}",
    )

    import texts

    lead = db.Lead(
        id=1, radar_id=1, user_id=USER, chat_id=-100, chat_title="Заказы IT",
        msg_id=1, author_id=7, author_name="Иван", author_user="ivan",
        text=items[0].text, score=verdicts[0].score, budget=30000,
        currency="RUB", urgency="high", stack=[], summary="Бот записи",
        verdict="order", judged=False, repeats=0, notified=False, created_at=0,
    )
    card = texts.lead_card(lead, None)
    check("карточка честно пишет, что модель не разбирала",
          "без разбора моделью" in card, card.splitlines()[0])
    check("и не показывает выдуманную оценку", "/100" not in card)

    # Поднятая планка не должна глушить ленту у тех, у кого ключа нет:
    # оценка от регулярок ей не подчиняется, а фильтр по бюджету — да.
    import radar as radar_mod

    strict = db.Radar(
        id=1, user_id=USER, account_id=1, title="Строгий",
        profile="боты", keywords="", min_budget=0, currency="RUB",
        min_score=95, status="running", seen=0, passed=0, found=0,
        note=None, created_at=0,
    )
    shown = []

    async def catch(_bot, _user_id, text):
        shown.append(text)

    saved = radar_mod._tell
    radar_mod._tell = catch
    try:
        for item, verdict in zip(items, verdicts):
            await radar_mod._store(None, strict, item, verdict, strict.min_score)
    finally:
        radar_mod._tell = saved

    check("высокая планка не глушит ленту без модели", len(shown) == 2,
          f"показано {len(shown)} из 2")
    check("найденное без модели записалось заказом",
          len(await db.leads(USER, verdict="order")) >= 2)


def check_wiring() -> None:
    print("\nСборка")
    check("роутер бота собран", handlers.router.name == "menu")
    import radar_ui

    check("роутер радара собран", radar_ui.router.name == "radar")
    # Радар обязан стоять впереди общего роутера: у handlers последним
    # висит хендлер на любое сообщение в личке, и команды радара он бы
    # съел — молча, без единой ошибки в логе.
    source = (config.BASE_DIR / "main.py").read_text(encoding="utf-8")
    check(
        "радар подключён раньше общего роутера",
        source.index("radar_ui.router") < source.index("handlers.router"),
    )
    check("воркер радара запускается", "radar.worker(bot)" in source)
    check("воркер радара останавливается", "scout)" in source)
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
        "POST /api/invoice/crypto",
        "POST /api/invoice/xrocket",
        "POST /api/paid-limit",
        "POST /api/admin/ban",
        "POST /api/tickets",
        "POST /api/ticket/read",
        "POST /api/ticket/send",
        "POST /api/ticket/close",
        "POST /api/material/upload",
        "POST /api/watch/create",
        "POST /api/watch/edit",
        "POST /api/watch/toggle",
        "POST /api/watch/delete",
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
        check_client_args()
        check_init_data()
        await check_broadcast()
        await check_broadcast_errors()
        await check_broadcast_limits()
        await check_folders()
        await check_tdata()
        await check_account_api()
        await check_payments()
        await check_xrocket()
        await check_custom_amount()
        await check_referrals()
        await check_materials()
        await check_admin()
        await check_custom_menu()
        await check_legal()
        await check_menu_buttons()
        await check_tariffs_message()
        await check_variables()
        await check_cast_buttons()
        await check_platega()
        await check_variants()
        await check_attached_media()
        await check_no_private()
        await check_tickets()
        await check_ban_and_faq()
        await check_paid_chats()
        await check_watches()
        await check_radar_filter()
        await check_radar_verdicts()
        await check_radar_storage()
        check_radar_card()
        await check_radar_offline()
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
