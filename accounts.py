"""Подключение аккаунта по номеру телефона (Telethon, MTProto).

Bot API так не умеет в принципе: бот не может писать первым, не может
войти по номеру и не видит чужих чатов. Рассылка от лица человека — это
всегда обычный аккаунт, а обычный аккаунт подключается только через
MTProto: номер → код из Telegram → при облачном пароле ещё и пароль.

Главная тонкость здесь одна, и она определяет всю конструкцию:
**phone_code_hash привязан к живому подключению**. Между «прислать код»
и «ввести код» нельзя ни закрыть клиент, ни создать новый — тот же код
на новом подключении Telegram не примет и ответит PhoneCodeExpired. То
есть незавершённый вход обязан висеть в памяти процесса, и веб-сервер
мини-аппа поэтому живёт в одном процессе с ботом, а не рядом.

Что здесь намеренно не делается:

* **Пароль не хранится.** Он уходит в Telegram и забывается вместе с
  записью о входе; в базу не попадает и в лог не пишется.
* **Сессия не пишется файлом.** StringSession держит ключ в памяти,
  оттуда он уходит в базу зашифрованным. Файлов .session с открытым
  ключом на диске не остаётся.
* **Аккаунты не регистрируются.** Если номер в Telegram не занят,
  подключение отклоняется: заводить новые аккаунты — это уже про
  массовую регистрацию, а не про рассылку со своего.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

import config
import crypto
import db

log = logging.getLogger("rassylka.accounts")

#: Как аккаунт увидит это подключение в своём списке активных сессий.
#: Названо честно и узнаваемо: человек должен понимать, что за сессия у
#: него появилась, и уметь отозвать её руками.
DEVICE_MODEL = "Rassylka"
APP_VERSION = "1.0"


class LoginError(Exception):
    """Ошибка входа с текстом, готовым к показу человеку.

    Отдельный тип, чтобы веб-слой не разбирал исключения Telethon сам:
    он показывает message, а решение о том, что человеку сказать,
    принимается здесь, рядом со знанием, что именно сломалось.
    """

    def __init__(
        self, message: str, *, retry_after: int = 0, restart: bool = False
    ) -> None:
        super().__init__(message)
        self.message = message
        #: Сколько секунд ждать (FloodWait). 0 — ждать не нужно.
        self.retry_after = retry_after
        #: True — начинать заново с номера: код или попытка уже мертвы.
        self.restart = restart


@dataclass
class Pending:
    """Незавершённый вход: живой клиент и то, что нужно для sign_in."""

    token: str
    user_id: int
    phone: str
    client: Any
    phone_code_hash: str
    created_at: float
    #: code — ждём код, password — код принят, нужен облачный пароль.
    stage: str = "code"
    #: Два одновременных «ввести код» с двух устройств не должны уйти в
    #: sign_in оба: второй получит PhoneCodeExpired и собьёт живой вход.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.created_at > config.LOGIN_TTL


_pending: dict[str, Pending] = {}


def normalize_phone(raw: str) -> str:
    """Номер в вид +79991234567. Пусто — если это не похоже на номер.

    Отдельно разбирается домашняя запись «8 999 …»: так номер набирают в
    России, и человек вводит его именно так, не задумываясь. Дописать к
    ней плюс нельзя — кода страны 8 не существует, и Telegram ответил бы
    «такого номера нет» на вполне рабочий номер. Одиннадцать цифр,
    начинающихся с восьмёрки, — это всегда +7.

    Десять цифр остаются как есть: в части стран это полный номер с
    кодом, и угадывать за человека код страны здесь уже нельзя.
    """
    digits = "".join(ch for ch in (raw or "") if ch.isdigit())
    # 10 цифр — это уже осмысленный номер; меньше просто опечатка, и
    # дёргать ради неё Telegram не надо.
    if not 10 <= len(digits) <= 15:
        return ""
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    return "+" + digits


def _client(phone: str):
    """Клиент на StringSession: ключ живёт в памяти, файлов не создаёт."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    return TelegramClient(
        StringSession(),
        config.MTPROTO_API_ID,
        config.MTPROTO_API_HASH,
        device_model=DEVICE_MODEL,
        system_version="Windows 10",
        app_version=APP_VERSION,
        lang_code="ru",
        system_lang_code="ru",
    )


async def _drop(token: str) -> None:
    """Убрать запись о входе и закрыть её клиент."""
    pending = _pending.pop(token, None)
    if pending is None:
        return
    try:
        await pending.client.disconnect()
    except Exception as error:
        log.debug("клиент незавершённого входа не закрылся: %s", error)


async def drop_user_logins(user_id: int) -> None:
    """Закрыть все незавершённые входы человека.

    Вызывается перед новым запросом кода: иначе каждая новая попытка
    оставляла бы в памяти подключённый клиент от прошлой.
    """
    for token in [t for t, p in _pending.items() if p.user_id == user_id]:
        await _drop(token)


async def sweep() -> int:
    """Выбросить просроченные входы. Возвращает, сколько выбросила."""
    dead = [token for token, pending in _pending.items() if pending.expired]
    for token in dead:
        await _drop(token)
    return len(dead)


async def sweeper() -> None:
    """Фоновая уборка незавершённых входов."""
    while True:
        await asyncio.sleep(60)
        try:
            gone = await sweep()
            if gone:
                log.info("просроченных входов закрыто: %s", gone)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            log.warning("уборка входов сорвалась: %s", error)


def pending_for(user_id: int) -> Pending | None:
    """Незавершённый вход человека, если он есть и ещё жив."""
    for pending in _pending.values():
        if pending.user_id == user_id and not pending.expired:
            return pending
    return None


def owner_of(token: str) -> int | None:
    """Кто начинал этот вход. None — такого входа нет."""
    pending = _pending.get(token or "")
    return pending.user_id if pending else None


def pending_count() -> int:
    return len(_pending)


async def close_all() -> None:
    """Закрыть все незавершённые входы. Вызывается при остановке бота.

    Каждый такой вход держит подключение к Telegram, и без этого процесс
    висит до таймаута сокетов.
    """
    for token in list(_pending):
        await _drop(token)


# --- шаг первый: запросить код ----------------------------------------


async def start(user_id: int, raw_phone: str) -> Pending:
    """Попросить Telegram прислать код на номер.

    Возвращает запись о входе: её token нужен на следующем шаге.
    """
    if not config.mtproto_ready():
        raise LoginError(
            "Подключение аккаунтов пока не настроено на сервере. "
            "Напишите в поддержку."
        )
    phone = normalize_phone(raw_phone)
    if not phone:
        raise LoginError("Не похоже на номер телефона. Пример: +7 999 123-45-67")

    if await db.count_accounts(user_id) >= config.MAX_ACCOUNTS:
        raise LoginError(
            f"Больше {config.MAX_ACCOUNTS} аккаунтов подключить нельзя. "
            "Отключите ненужный и попробуйте снова."
        )

    # Тормоз до обращения к Telegram: частые запросы кода он наказывает
    # FloodWait не боту, а самому номеру — на часы.
    if await db.code_requests_last_hour(user_id) >= config.CODE_REQUESTS_PER_HOUR:
        raise LoginError(
            "Слишком много запросов кода за последний час. Подождите час — "
            "иначе Telegram сам заблокирует вход по этому номеру на сутки."
        )

    await drop_user_logins(user_id)

    from telethon import errors

    client = _client(phone)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except errors.PhoneNumberInvalidError:
        await client.disconnect()
        raise LoginError("Telegram не знает такого номера. Проверьте его.")
    except errors.PhoneNumberBannedError:
        await client.disconnect()
        raise LoginError("Этот номер заблокирован в Telegram — войти не получится.")
    except errors.PhoneNumberFloodError:
        await client.disconnect()
        raise LoginError(
            "С этого номера сегодня слишком много попыток входа. "
            "Попробуйте завтра."
        )
    except errors.FloodWaitError as error:
        await client.disconnect()
        raise LoginError(
            f"Telegram просит подождать {_human_wait(error.seconds)} "
            "перед следующей попыткой.",
            retry_after=error.seconds,
        )
    except errors.ApiIdInvalidError:
        await client.disconnect()
        log.error("MTPROTO_API_ID / MTPROTO_API_HASH не приняты Telegram")
        raise LoginError(
            "Ключи Telegram на сервере не приняты. Это не ваша ошибка — "
            "напишите в поддержку."
        )
    except Exception as error:
        await client.disconnect()
        log.exception("запрос кода на %s сорвался", _mask(phone))
        raise LoginError(f"Не удалось запросить код: {type(error).__name__}")

    await db.log_code_request(user_id, phone)

    pending = Pending(
        token=config.new_token(),
        user_id=user_id,
        phone=phone,
        client=client,
        phone_code_hash=sent.phone_code_hash,
        created_at=time.monotonic(),
    )
    _pending[pending.token] = pending
    log.info("код запрошен: %s (%s)", _mask(phone), user_id)
    return pending


# --- шаг второй: код, при необходимости пароль ------------------------


def _get(token: str) -> Pending:
    pending = _pending.get(token or "")
    if pending is None:
        raise LoginError(
            "Вход уже не активен. Начните с номера заново.", restart=True
        )
    if pending.expired:
        raise LoginError("Код устарел. Запросите новый.", restart=True)
    return pending


async def submit_code(token: str, code: str) -> db.Account | None:
    """Завершить вход кодом. None — код принят, но нужен облачный пароль."""
    pending = _get(token)
    code = "".join(ch for ch in (code or "") if ch.isdigit())
    if not code:
        raise LoginError("Введите код из Telegram — только цифры.")

    from telethon import errors

    async with pending.lock:
        try:
            await pending.client.sign_in(
                phone=pending.phone,
                code=code,
                phone_code_hash=pending.phone_code_hash,
            )
        except errors.SessionPasswordNeededError:
            pending.stage = "password"
            return None
        except errors.PhoneCodeInvalidError:
            raise LoginError("Код неверный. Проверьте и введите ещё раз.")
        except errors.PhoneCodeEmptyError:
            raise LoginError("Код не введён.")
        except errors.PhoneCodeExpiredError:
            await _drop(token)
            raise LoginError("Код устарел. Запросите новый.", restart=True)
        except errors.PhoneNumberUnoccupiedError:
            await _drop(token)
            raise LoginError(
                "На этом номере нет аккаунта Telegram. Подключить можно "
                "только уже существующий аккаунт.",
                restart=True,
            )
        except errors.AuthRestartError:
            await _drop(token)
            raise LoginError("Telegram просит начать вход заново.", restart=True)
        except errors.FloodWaitError as error:
            await _drop(token)
            raise LoginError(
                f"Telegram просит подождать {_human_wait(error.seconds)}.",
                retry_after=error.seconds,
                restart=True,
            )
        except Exception as error:
            log.exception("вход по коду сорвался: %s", _mask(pending.phone))
            raise LoginError(f"Войти не удалось: {type(error).__name__}")

        return await _finish(token, pending)


async def submit_password(token: str, password: str) -> db.Account:
    """Завершить вход облачным паролем (двухфакторная защита)."""
    pending = _get(token)
    if pending.stage != "password":
        raise LoginError("Пароль сейчас не нужен — введите код из Telegram.")
    if not password:
        raise LoginError("Введите облачный пароль.")

    from telethon import errors

    async with pending.lock:
        try:
            await pending.client.sign_in(password=password)
        except errors.PasswordHashInvalidError:
            raise LoginError("Пароль неверный.")
        except errors.FloodWaitError as error:
            await _drop(token)
            raise LoginError(
                f"Telegram просит подождать {_human_wait(error.seconds)}.",
                retry_after=error.seconds,
                restart=True,
            )
        except Exception as error:
            log.exception("вход по паролю сорвался: %s", _mask(pending.phone))
            raise LoginError(f"Войти не удалось: {type(error).__name__}")

        account = await _finish(token, pending)
        assert account is not None
        return account


async def _finish(token: str, pending: Pending) -> db.Account:
    """Забрать сессию, зашифровать, записать и закрыть клиент."""
    from telethon.sessions import StringSession

    try:
        me = await pending.client.get_me()
    except Exception as error:
        log.warning("кто вошёл — узнать не удалось: %s", error)
        me = None

    # Строка сессии снимается с любой сессии: ей нужны ключ, номер
    # дата-центра и адрес — они одинаковые при любом способе хранения.
    session = StringSession.save(pending.client.session)
    account_id = await db.save_account(
        pending.user_id,
        pending.phone,
        crypto.encrypt(session),
        tg_id=getattr(me, "id", None),
        name=" ".join(
            part
            for part in (
                getattr(me, "first_name", None),
                getattr(me, "last_name", None),
            )
            if part
        )
        or None,
        username=getattr(me, "username", None),
    )
    await _drop(token)
    log.info(
        "аккаунт подключён: %s (%s), человек %s",
        _mask(pending.phone),
        getattr(me, "username", None) or getattr(me, "id", "?"),
        pending.user_id,
    )
    saved = await db.account(pending.user_id, account_id)
    if saved is None:  # запись пропала между INSERT и SELECT — не бывает
        raise LoginError("Аккаунт вошёл, но не записался. Напишите в поддержку.")
    return saved


async def cancel(token: str) -> None:
    await _drop(token)


# --- работа с уже подключённым аккаунтом ------------------------------


async def client_for(account: db.Account):
    """Подключённый клиент по сохранённой сессии.

    Ради этой функции всё и делалось: рассылка будет брать клиент отсюда.
    Закрывать его — забота вызывающего.
    """
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    session = crypto.decrypt(account.session)
    if not session:
        raise LoginError(
            "Сессия этого аккаунта больше не читается — подключите его заново.",
            restart=True,
        )
    client = TelegramClient(
        StringSession(session),
        config.MTPROTO_API_ID,
        config.MTPROTO_API_HASH,
        device_model=DEVICE_MODEL,
        system_version="Windows 10",
        app_version=APP_VERSION,
        lang_code="ru",
        system_lang_code="ru",
    )
    await client.connect()
    return client


async def verify(user_id: int, account_id: int) -> db.Account | None:
    """Живой ли аккаунт: подключиться и спросить, кто мы.

    Статус в базе после этого честный. Проверка не делается при каждом
    открытии приложения намеренно: это поход в Telegram на каждый
    аккаунт, и на списке из пяти номеров приложение открывалось бы
    секунды. Здесь — по кнопке и сразу после подключения.
    """
    account = await db.account(user_id, account_id)
    if account is None:
        return None

    client = None
    try:
        client = await client_for(account)
        if not await client.is_user_authorized():
            await db.mark_account(
                account.id, "dead", "сессия отозвана в Telegram"
            )
        else:
            me = await client.get_me()
            await db.save_account(
                user_id,
                account.phone,
                account.session,
                tg_id=getattr(me, "id", None),
                name=" ".join(
                    part
                    for part in (
                        getattr(me, "first_name", None),
                        getattr(me, "last_name", None),
                    )
                    if part
                )
                or None,
                username=getattr(me, "username", None),
            )
    except LoginError:
        await db.mark_account(account.id, "dead", "сессия не читается")
    except Exception as error:
        log.warning("проверка аккаунта %s сорвалась: %s", account_id, error)
        await db.mark_account(account.id, "dead", type(error).__name__)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    return await db.account(user_id, account_id)


async def forget(user_id: int, account_id: int) -> bool:
    """Отключить аккаунт: разлогинить в Telegram и убрать из базы.

    Разлогинить важно. Если просто удалить запись, сессия останется
    живой в списке активных у человека — и ключ, который мы уже не
    храним, будет продолжать действовать.
    """
    account = await db.account(user_id, account_id)
    if account is None:
        return False

    client = None
    try:
        client = await client_for(account)
        if await client.is_user_authorized():
            await client.log_out()
    except Exception as error:
        # Не смогли отозвать — всё равно удаляем запись: держать у себя
        # сессию, которую человек попросил убрать, нельзя.
        log.warning("сессию %s отозвать не удалось: %s", account_id, error)
    finally:
        if client is not None:
            try:
                await client.disconnect()
            except Exception:
                pass

    return await db.delete_account(user_id, account_id)


# --- мелочи -----------------------------------------------------------


def _mask(phone: str) -> str:
    """Номер для лога: середина скрыта.

    Логи читают и пересылают в переписке, полный номер человека там не
    нужен.
    """
    return phone[:5] + "***" + phone[-2:] if len(phone) > 8 else "***"


def _human_wait(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} сек"
    if seconds < 3600:
        return f"{seconds // 60} мин"
    return f"{seconds // 3600} ч"
