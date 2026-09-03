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
import crypto  # noqa: E402
import db  # noqa: E402
import handlers  # noqa: E402
import keyboards  # noqa: E402
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
