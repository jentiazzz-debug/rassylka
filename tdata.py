"""Импорт аккаунтов из tdata — папки Telegram Desktop.

Что такое tdata. Telegram Desktop хранит в ней ключи авторизации своих
аккаунтов. Отдать её кому-то — то же самое, что отдать сам аккаунт:
по ней входят без номера, кода и пароля. Поэтому здесь всё устроено
так, чтобы она не задерживалась: архив распаковывается во временную
папку, из него достаются строки сессий, и папка стирается — в том числе
когда импорт сорвался на середине.

Разбирает формат opentele. Он тянет за собой PyQt5 (tdata написана
сериализацией Qt), поэтому импортируется лениво: без установленной
библиотеки должна отваливаться одна эта возможность, а не весь бот.

**Ключи сохраняются вместе с сессией.** Telegram Desktop выдавал её под
свой api_id, и подключаться к ней нашим — верный способ её потерять:
для Telegram это выглядит как угон, и сессию отзывают. Поэтому при
импорте параметры запоминаются, и дальше аккаунт всегда ходит с ними
(см. accounts._api_params).
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import zipfile
from pathlib import Path

import accounts
import config
import crypto
import db

log = logging.getLogger("rassylka.tdata")

#: Потолок распакованного архива. Обычная tdata — единицы мегабайт;
#: всё, что сильно больше, это либо не tdata, либо архив-бомба, которым
#: забивают диск: сжатые нули разворачиваются в гигабайты.
MAX_UNPACKED = 300 * 1024 * 1024

#: Потолок числа файлов в архиве — защита от того же, но количеством.
MAX_ENTRIES = 20000


class TdataError(Exception):
    """Ошибка импорта с текстом, готовым к показу человеку."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def available() -> bool:
    """Установлена ли библиотека разбора tdata."""
    try:
        import opentele  # noqa: F401
    except Exception:
        return False
    return True


# --- распаковка -------------------------------------------------------


def _safe_extract(archive: Path, target: Path) -> None:
    """Распаковать архив, не дав ему вылезти за пределы своей папки.

    Имя внутри zip — это просто строка, и туда кладут «../../etc/passwd»
    или абсолютный путь. Такой архив при наивной распаковке пишет куда
    угодно на диске. Проверяем каждый путь после склейки: он обязан
    остаться внутри target.
    """
    try:
        with zipfile.ZipFile(archive) as bundle:
            entries = bundle.infolist()
            if len(entries) > MAX_ENTRIES:
                raise TdataError("В архиве слишком много файлов — это не tdata.")

            total = 0
            for entry in entries:
                total += entry.file_size
                if total > MAX_UNPACKED:
                    raise TdataError(
                        "Архив слишком большой в распакованном виде. "
                        "Заархивируйте только папку tdata."
                    )

                name = entry.filename.replace("\\", "/")
                if name.startswith("/") or ".." in Path(name).parts:
                    log.warning("подозрительный путь в архиве: %s", name)
                    continue
                # Симлинки в zip хранятся как файлы со специальным
                # режимом. Распаковать их — значит позволить архиву
                # ссылаться наружу; нам они не нужны в принципе.
                if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                    continue

                destination = (target / name).resolve()
                if not str(destination).startswith(str(target.resolve())):
                    log.warning("путь вырывается из папки: %s", name)
                    continue

                if entry.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(entry) as source, open(destination, "wb") as out:
                    shutil.copyfileobj(source, out, 1024 * 1024)
    except zipfile.BadZipFile:
        raise TdataError("Это не zip-архив. Заархивируйте папку tdata в zip.")


def find_tdata(root: Path) -> Path | None:
    """Найти саму tdata в распакованном дереве.

    Архивируют по-разному: кто-то саму папку tdata, кто-то её содержимое,
    кто-то всю папку Telegram Desktop целиком. Опознаём по файлу с
    ключом — он лежит ровно в корне tdata и называется key_data
    (иногда с цифрой на конце: Desktop пишет файлы в два слота).
    """
    if any(root.glob("key_data*")):
        return root
    for path in sorted(root.rglob("key_data*")):
        if path.is_file():
            return path.parent
    return None


# --- импорт -----------------------------------------------------------


def _api_dict(api) -> dict:
    """Ключи и устройство — в том виде, в каком они лягут в базу."""
    return {
        "api_id": int(api.api_id),
        "api_hash": str(api.api_hash),
        "device_model": getattr(api, "device_model", None),
        "system_version": getattr(api, "system_version", None),
        "app_version": getattr(api, "app_version", None),
        "lang_code": getattr(api, "lang_code", None) or "en",
        "system_lang_code": getattr(api, "system_lang_code", None) or "en-US",
    }


async def _one_account(user_id: int, entry, api) -> dict:
    """Достать сессию одного аккаунта из tdata и записать его."""
    from telethon.sessions import StringSession

    from opentele.api import UseCurrentSession

    client = await entry.ToTelethon(
        session=StringSession(),
        # Именно текущая сессия, а не новая: заводить новую — это ещё
        # один вход в списке устройств и лишний повод для Telegram
        # присмотреться к аккаунту.
        flag=UseCurrentSession,
        api=api,
    )
    try:
        await client.connect()
        if not await client.is_user_authorized():
            return {"ok": False, "error": "сессия недействительна"}
        me = await client.get_me()
        phone = accounts.normalize_phone(getattr(me, "phone", "") or "")
        if not phone:
            # Без номера запись не завести: он ключ записи в базе.
            return {"ok": False, "error": "у аккаунта не читается номер"}

        name = " ".join(
            part
            for part in (
                getattr(me, "first_name", None),
                getattr(me, "last_name", None),
            )
            if part
        ) or None

        await db.save_account(
            user_id,
            phone,
            crypto.encrypt(StringSession.save(client.session)),
            tg_id=getattr(me, "id", None),
            name=name,
            username=getattr(me, "username", None),
            api=_api_dict(api),
            source="tdata",
        )
        return {
            "ok": True,
            "phone": phone,
            "name": name or phone,
            "username": getattr(me, "username", None),
        }
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass


async def import_zip(user_id: int, archive: Path, passcode: str = "") -> dict:
    """Распаковать архив, забрать все аккаунты из tdata, записать их.

    Возвращает, что удалось и что нет, по каждому аккаунту отдельно:
    в одной tdata их бывает несколько, и один сломанный не повод
    отказать в остальных.
    """
    if not available():
        raise TdataError(
            "Импорт из tdata не настроен на сервере. Напишите в поддержку."
        )

    workdir = Path(tempfile.mkdtemp(prefix="tdata-", dir=config.DATA_DIR))
    try:
        _safe_extract(archive, workdir)
        root = find_tdata(workdir)
        if root is None:
            raise TdataError(
                "В архиве нет папки tdata. Нужна именно она — она лежит "
                "рядом с Telegram.exe."
            )

        from opentele.api import API
        from opentele.exception import OpenTeleException
        from opentele.td import TDesktop

        try:
            desktop = TDesktop(str(root), passcode=passcode or None)
        except OpenTeleException as error:
            raise TdataError(_explain(error))
        except Exception as error:
            log.exception("tdata не разобралась")
            raise TdataError(f"Не удалось прочитать tdata: {type(error).__name__}")

        if not desktop.isLoaded() or not desktop.accounts:
            raise TdataError(
                "В tdata не нашлось ни одного аккаунта. Если на Telegram "
                "Desktop стоит локальный код-пароль, введите его."
            )

        # Ключи и устройство генерируются один раз на импорт и с
        # привязкой к человеку: у одного аккаунта параметры устройства
        # должны быть постоянными, иначе в списке сессий у него каждый
        # раз новое устройство.
        api = API.TelegramDesktop.Generate(unique_id=str(user_id))

        free = config.MAX_ACCOUNTS - await db.count_accounts(user_id)
        added, failed = [], []
        for entry in desktop.accounts:
            if free <= 0:
                failed.append(
                    {"error": f"больше {config.MAX_ACCOUNTS} аккаунтов нельзя"}
                )
                break
            try:
                result = await _one_account(user_id, entry, api)
            except Exception as error:
                log.exception("аккаунт из tdata не импортировался")
                result = {"ok": False, "error": type(error).__name__}
            if result.get("ok"):
                added.append(result)
                free -= 1
            else:
                failed.append(result)

        log.info(
            "tdata человека %s: добавлено %s, не вышло %s",
            user_id, len(added), len(failed),
        )
        return {"added": added, "failed": failed}
    finally:
        # Ключи чужих аккаунтов не должны пережить импорт ни на минуту —
        # ни при удаче, ни при ошибке.
        shutil.rmtree(workdir, ignore_errors=True)


def _explain(error: Exception) -> str:
    """Ошибку opentele — человеческим языком."""
    name = type(error).__name__
    if name == "PasswordIncorrect":
        return "Код-пароль не подошёл."
    if name == "NoPasswordProvided":
        return (
            "На этой tdata стоит локальный код-пароль Telegram Desktop — "
            "введите его."
        )
    if name in {"TFileNotFound", "TDataInvalid", "TDataBadMagic"}:
        return (
            "Папка не похожа на tdata. Заархивируйте именно её — ту, что "
            "лежит рядом с Telegram.exe."
        )
    if name == "MaxAccountLimit":
        return "В tdata слишком много аккаунтов."
    return f"tdata не читается: {name}"
