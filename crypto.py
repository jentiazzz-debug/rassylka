"""Шифрование строк сессий (Fernet).

Строка сессии Telethon — это не «логин и пароль», а уже готовый ключ
авторизации: кто её взял, тот читает переписку и пишет от имени
аккаунта, не зная ни пароля, ни кода. Поэтому в базу она попадает только
зашифрованной, а ключ живёт отдельно — в переменной окружения или в
файле (см. config.session_key).

Fernet берётся из cryptography — библиотеки, которую и так тянет за
собой telethon, так что отдельной зависимости здесь нет.
"""

from __future__ import annotations

import logging

import config

log = logging.getLogger("rassylka.crypto")

_box = None


def _fernet():
    """Ленивый Fernet: ключ читается (и, если нужно, создаётся) один раз."""
    global _box
    if _box is None:
        from cryptography.fernet import Fernet

        _box = Fernet(config.session_key())
    return _box


def encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode()).decode()


def decrypt(blob: str) -> str | None:
    """Расшифровать строку сессии. None — если не получилось.

    Не получиться может по одной причине: ключ уже не тот, что был при
    записи (переехали без файла session.key, сменили SESSION_KEY).
    Падать здесь нельзя — иначе один старый аккаунт в базе роняет
    открытие приложения целиком. Вернём None, и такой аккаунт покажется
    в списке как отключённый, с предложением подключить заново.
    """
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(blob.encode()).decode()
    except (InvalidToken, ValueError, TypeError) as error:
        log.warning("сессию не расшифровать (сменился ключ?): %s", error)
        return None


def ready() -> bool:
    """Есть ли чем шифровать. Проверяется на старте, а не при первом входе."""
    try:
        probe = encrypt("проверка")
        return decrypt(probe) == "проверка"
    except Exception as error:  # нет cryptography или битый ключ
        log.error("шифрование сессий не работает: %s", error)
        return False
