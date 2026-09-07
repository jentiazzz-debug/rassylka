"""Настройки: бот, мини-апп, ключи MTProto, шифрование сессий.

Всё читается из .env рядом с main.py. Общий .env в корне папки ботов
намеренно не подхватывается: там лежит токен другого бота, а поллинг с
одним токеном может вести только один процесс.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

log = logging.getLogger("rassylka.config")

#: Хостинги отдают под данные отдельный том и сообщают о нём переменной
#: DATA_DIR. Не слушать её нельзя: база ляжет рядом с кодом и сотрётся на
#: первом же передеплое — вместе со строками сессий, то есть человеку
#: придётся заново подключать все аккаунты по номеру и коду.
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

WEBAPP_DIR = BASE_DIR / "webapp"


def _int(name: str, default: str) -> int:
    raw = (os.getenv(name) or default).strip()
    return int(raw) if raw.lstrip("-").isdigit() else int(default)


def _float(name: str, default: str) -> float:
    try:
        return float((os.getenv(name) or default).replace(",", "."))
    except (ValueError, AttributeError):
        return float(default)


def _bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on", "да"}


def _ints(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if chunk.lstrip("-").isdigit():
            out.add(int(chunk))
    return out


# --- бот --------------------------------------------------------------

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DB_PATH = Path(os.getenv("DB_PATH") or DATA_DIR / "rassylka.db")

#: ADMIN_ID и OWNER_ID — синонимы: так называется одно и то же в разных
#: ботах этой папки, и путать их при переносе .env не хочется.
ADMIN_IDS = (
    _ints(os.getenv("ADMIN_IDS"))
    | _ints(os.getenv("ADMIN_ID"))
    | _ints(os.getenv("OWNER_ID"))
)


def _support_url() -> str:
    """Ссылка на поддержку: из готового адреса либо из ника."""
    raw = (os.getenv("SUPPORT_URL") or "").strip()
    if raw:
        return raw
    nick = (os.getenv("SUPPORT_USERNAME") or "").strip().lstrip("@")
    return f"https://t.me/{nick}" if nick else ""


SUPPORT_URL = _support_url()


# --- мини-апп ---------------------------------------------------------

#: Публичный адрес мини-аппа. Telegram открывает WebApp только по https
#: и только по адресу, который знает клиент, — localhost в кнопку не
#: положить. На своей машине это туннель (cloudflared / ngrok), на
#: хостинге — домен. Пусто — кнопка «Открыть приложение» не появится,
#: и бот скажет об этом в лог при старте.
WEBAPP_URL = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")

#: Свой веб-сервер: он же раздаёт мини-апп, он же отвечает на его
#: запросы. Отдельный процесс не нужен — Telethon-клиенты незавершённых
#: входов живут в памяти, и разносить их с API по разным процессам нельзя.
HOST = (os.getenv("HOST") or "0.0.0.0").strip()
PORT = _int("PORT", "8080")

#: Сколько секунд initData считается свежей. Telegram подписывает её при
#: открытии приложения, и подпись не истекает сама — без своей проверки
#: один раз подсмотренная строка работала бы как вечный пароль.
INITDATA_TTL = _int("INITDATA_TTL", "86400")


# --- аккаунты для рассылки --------------------------------------------

MTPROTO_API_ID = _int("MTPROTO_API_ID", "0")
MTPROTO_API_HASH = (os.getenv("MTPROTO_API_HASH") or "").strip()

#: Сколько аккаунтов может подключить один человек. Несколько номеров —
#: это не про объём в одного, а про то, что лимиты Telegram выдаёт
#: персонально: пока один ждёт FloodWait, работает следующий.
MAX_ACCOUNTS = max(1, _int("MAX_ACCOUNTS", "5"))

#: Сколько живёт незавершённый вход. Между «прислать код» и «ввести код»
#: в памяти висит подключённый Telethon-клиент: phone_code_hash привязан
#: к его ключу авторизации, и на новом подключении тот же код не примут.
#: Долго держать такие клиенты не нужно — код всё равно протухает у
#: Telegram за несколько минут.
LOGIN_TTL = _int("LOGIN_TTL", "600")

#: Сколько раз в час один человек может просить код. Каждый запрос
#: Telegram считает попыткой входа и на частых выдаёт FloodWait на часы —
#: уже не боту, а номеру человека. Поэтому тормоз стоит у нас, до
#: обращения к Telegram.
CODE_REQUESTS_PER_HOUR = max(1, _int("CODE_REQUESTS_PER_HOUR", "5"))

#: Потолок размера файла, который можно загрузить в «Избранное» из
#: приложения. Больше Telegram от обычного аккаунта и не примет: там
#: потолок 2 ГБ, но всё, что крупнее полусотни мегабайт, у нас сначала
#: пройдёт через диск сервера — а это чужие файлы на нашем диске.
MAX_UPLOAD_MB = max(1, _int("MAX_UPLOAD_MB", "32"))

#: Потолок размера архива с tdata, мегабайты. Обычная tdata — единицы
#: мегабайт; запас нужен тем, у кого в ней несколько аккаунтов и кэш.
MAX_TDATA_MB = max(1, _int("MAX_TDATA_MB", "64"))

#: Ключ шифрования строк сессий (Fernet, base64). Строка сессии — это
#: полный доступ к аккаунту: с ней читают переписку и пишут от его имени.
#: В базе она лежит только зашифрованной.
#:
#: Своего ключа в .env нет — генерируем и кладём файлом рядом с базой.
#: Ключ и база в одном томе означают, что доступ к тому даёт и то и
#: другое: на хостинге ключ лучше держать в переменной окружения, а файл
#: оставить как режим «запустил и работает».
SESSION_KEY = (os.getenv("SESSION_KEY") or "").strip()
SESSION_KEY_FILE = Path(os.getenv("SESSION_KEY_FILE") or DATA_DIR / "session.key")


# --- подписка ---------------------------------------------------------

#: Бесплатный пробный период, дней. Считается от первого /start.
TRIAL_DAYS = max(0, _int("TRIAL_DAYS", "5"))

#: Подпись, которая дописывается в конец каждого сообщения рассылки на
#: бесплатном тарифе. На платном — не дописывается.
FREE_FOOTER = (os.getenv("FREE_FOOTER") or "").strip()


def _plans(raw: str) -> list[dict]:
    """Тарифы в виде «дни:звёзды», через запятую."""
    out: list[dict] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk or ":" not in chunk:
            continue
        days, _, stars = chunk.partition(":")
        if days.strip().isdigit() and stars.strip().isdigit():
            out.append({"days": int(days), "stars": int(stars)})
    return out


#: Тарифы подписки, «дни:монеты». Подписка покупается за монеты, а не
#: напрямую за звёзды: монеты приходят и от приглашённых тоже, и через
#: них две дороги к подписке сходятся в одну.
PLANS = _plans(os.getenv("PLANS") or "1:30,7:100,30:200")

#: Сколько звёзд стоит одна монета. Оплата звёздами — единственный
#: способ взять деньги внутри мини-аппа, не уводя человека на сторонний
#: сайт, и единственный, который Telegram разрешает для цифровых услуг.
STARS_PER_COIN = max(1, _int("STARS_PER_COIN", "1"))

def _packs(raw: str) -> list[dict]:
    """Пачки монет: «монеты» или «монеты:цена в звёздах».

    Вторая форма нужна для скидки: 500 монет за 475 звёзд. Скидка потом
    считается сама, из разницы с обычной ценой, — отдельным полем её
    задавать нельзя, иначе однажды она разъедется с настоящей.
    """
    out: list[dict] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        coins, _, stars = chunk.partition(":")
        if not coins.strip().isdigit():
            continue
        amount = int(coins)
        price = int(stars) if stars.strip().isdigit() else amount * STARS_PER_COIN
        out.append({"coins": amount, "stars": max(1, price)})
    return out


#: Пачки монет на покупку. Пример со скидкой: 200:190,500:460.
COIN_PACKS = _packs(
    os.getenv("COIN_PACKS") or "50,100,200:190,500:460"
) or _packs("50,100,200,500")

#: Какую пачку пометить как популярную. 0 — не помечать ни одну.
COIN_PACK_POPULAR = _int("COIN_PACK_POPULAR", "100")

#: Границы для «своего количества». Нижняя — чтобы счёт на одну монету
#: не съедал комиссию, верхняя — чтобы опечатка в поле не выставила счёт
#: на миллион звёзд.
COIN_MIN = max(1, _int("COIN_MIN", "10"))
COIN_MAX = max(COIN_MIN, _int("COIN_MAX", "10000"))

#: Как называется внутренняя валюта.
COIN_NAME = (os.getenv("COIN_NAME") or "Slx").strip()

#: Почём монета в рублях. Оплата рублями идёт через Platega; звёзды и
#: рубли живут параллельно, и курс у них свой.
RUB_PER_COIN = _float("RUB_PER_COIN", "2")

#: Нижний порог рублёвого счёта: у эквайринга есть своя минимальная
#: сумма, и счёт ниже неё просто не откроется на их стороне.
RUB_MIN = _float("RUB_MIN", "10")

#: Пачки монет для оплаты рублями. Формат тот же, что у звёздных:
#: «монеты» или «монеты:цена в рублях».
def _rub_packs(raw: str) -> list[dict]:
    out: list[dict] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        coins, _, price = chunk.partition(":")
        if not coins.strip().isdigit():
            continue
        amount = int(coins)
        try:
            rub = float(price) if price.strip() else amount * RUB_PER_COIN
        except ValueError:
            rub = amount * RUB_PER_COIN
        out.append({"coins": amount, "rub": round(max(1.0, rub), 2)})
    return out


RUB_PACKS = _rub_packs(
    os.getenv("RUB_PACKS") or "50,100,200:380,500:920"
) or _rub_packs("50,100,200,500")


# --- Platega: оплата рублями ------------------------------------------

#: Ключи из личного кабинета Platega, раздел «Настройки».
PLATEGA_MERCHANT = (os.getenv("PLATEGA_MERCHANT_ID") or "").strip()
PLATEGA_SECRET = (os.getenv("PLATEGA_SECRET") or "").strip()
PLATEGA_API = (
    os.getenv("PLATEGA_API") or "https://app.platega.io"
).strip().rstrip("/")

#: Путь, на который Platega шлёт callback об оплате. Его же вписывают в
#: личном кабинете: Настройки → Callback URLs.
PLATEGA_CALLBACK_PATH = "/platega/callback"


def platega_ready() -> bool:
    return bool(PLATEGA_MERCHANT and PLATEGA_SECRET)


# --- CryptoBot: оплата криптой ----------------------------------------

#: Токен приложения из @CryptoBot: Crypto Pay → Create App.
CRYPTO_TOKEN = (os.getenv("CRYPTO_PAY_TOKEN") or "").strip()
CRYPTO_API = (
    os.getenv("CRYPTO_PAY_API") or "https://pay.crypt.bot"
).strip().rstrip("/")

#: Сколько монет даёт доллар.
COINS_PER_USD = max(1, _int("COINS_PER_USD", "50"))

#: Нижний порог счёта у криптокошельков. Технически они принимают и
#: центы, но счёт на $0.20 — это комиссия сети больше самой оплаты.
CRYPTO_MIN_USD = _float("CRYPTO_MIN_USD", "0.5")


def _crypto_packs(raw: str) -> list[dict]:
    """Пачки за доллары: «доллары» или «доллары:монеты»."""
    out: list[dict] = []
    for chunk in raw.replace(";", ",").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        usd, _, coins = chunk.partition(":")
        try:
            price = float(usd)
        except ValueError:
            continue
        amount = int(coins) if coins.strip().isdigit() else round(price * COINS_PER_USD)
        if amount > 0 and price > 0:
            out.append({"usd": round(price, 2), "coins": amount})
    return out


#: По умолчанию 1, 5, 10 и 25 долларов по курсу COINS_PER_USD.
CRYPTO_PACKS = _crypto_packs(os.getenv("CRYPTO_PACKS") or "1,5,10,25")


# --- xRocket: второй криптокошелёк ------------------------------------

#: Ключ приложения из @xRocket: xRocket Pay → Create app. Пусто — оплата
#: через xRocket выключена, остальные способы работают как работали.
XROCKET_TOKEN = (os.getenv("XROCKET_KEY") or "").strip()
XROCKET_API = (
    os.getenv("XROCKET_API") or "https://pay.xrocket.tg"
).strip().rstrip("/")

#: В чём выставлять счёт. USDT привязан к доллару, и цена в приложении
#: совпадает с той, что человек увидит в кошельке. Список того, что
#: xRocket принимает, отдаёт его же GET /currencies/available.
XROCKET_ASSET = (os.getenv("XROCKET_ASSET") or "USDT").strip().upper()


def xrocket_ready() -> bool:
    return bool(XROCKET_TOKEN)


# --- автокомментарии под постами каналов ------------------------------

#: Как часто заглядывать в канал за новыми постами. Реже, чем тик
#: рассылки: пост выходит не каждую минуту, а каждый заход — это запрос
#: к Telegram от лица аккаунта.
COMMENT_POLL = max(15, _int("COMMENT_POLL", "45"))

#: Сколько пропущенных постов разбирать. Если бота не было сутки, а
#: канал постил каждый час, ответить разом на все двадцать — это ровно
#: тот всплеск, за который аккаунт и ограничивают.
COMMENT_CATCHUP = max(1, _int("COMMENT_CATCHUP", "3"))

#: Нижняя планка задержки перед ответом.
#:
#: Ноль по умолчанию — и это осознанный выбор в пользу дела. Ответ через
#: паузу выглядит человечнее, но раздачи «первым десяти» именно этой
#: паузой и проигрываются: пока мы выжидаем правдоподобные полминуты,
#: подарки разбирают. Кому важнее незаметность, ставит здесь своё число,
#: и оно станет нижней границей для всех наблюдений.
COMMENT_MIN_DELAY = max(0, _int("COMMENT_MIN_DELAY", "0"))

#: Сколько раз переспросить Telegram про обсуждение поста и с какой
#: паузой. Группа обсуждений подхватывает свежий пост не мгновенно: при
#: ответе в ту же секунду Telegram отвечает «нет такого сообщения», и
#: без пары повторов мгновенный режим ломался бы именно на самых
#: быстрых постах — тех, ради которых он и сделан.
COMMENT_RETRIES = max(1, _int("COMMENT_RETRIES", "4"))
COMMENT_RETRY_PAUSE = _float("COMMENT_RETRY_PAUSE", "1.5")

#: Сколько наблюдений можно завести одному человеку.
MAX_WATCHES = max(1, _int("MAX_WATCHES", "10"))


# --- платные сообщения ------------------------------------------------

#: Самый большой потолок, который человек может себе поставить. Не
#: забота о наших деньгах — о его: звёзды списываются с подключённого
#: аккаунта, и опечатка в поле не должна стоить кошелька.
PAID_MAX_STARS = max(1, _int("PAID_MAX_STARS", "100"))


# --- документы и реквизиты --------------------------------------------

#: Кто оказывает услугу. Заполняется, но по умолчанию **не
#: показывается**: банк на согласовании просил убрать из бота и
#: документов сведения об ИП, ООО и ИНН. После регистрации кассы
#: показ включается одной переменной — переписывать документы не надо.
LEGAL_NAME = (os.getenv("LEGAL_NAME") or "").strip()
LEGAL_INN = (os.getenv("LEGAL_INN") or "").strip()
LEGAL_EMAIL = (os.getenv("LEGAL_EMAIL") or "").strip()

#: Показывать ли ИП/ООО и ИНН в документах.
LEGAL_SHOW_REQUISITES = _bool("LEGAL_SHOW_REQUISITES", False)

#: Кодовое слово проверяющего. Пока оно задано, видно на всех страницах
#: документов и в боте — так проверяющий сразу убеждается, что смотрит
#: именно тот сервис. После регистрации кассы переменная очищается.
REVIEW_CODE = (os.getenv("REVIEW_CODE") or "").strip()

#: Название сервиса в документах.
SERVICE_NAME = (os.getenv("SERVICE_NAME") or "Solutions Рассылка").strip()

#: Дата вступления документов в силу. Пусто — берётся сегодняшняя, но
#: лучше зафиксировать: банк смотрит на дату редакции.
LEGAL_DATE = (os.getenv("LEGAL_DATE") or "").strip()


def legal_ready() -> bool:
    """Есть ли по кому связаться. Реквизиты сюда не входят намеренно:
    показывать их банк на согласовании просил перестать."""
    return bool(LEGAL_EMAIL or SUPPORT_URL)


#: Сколько монет получает пригласивший, когда приглашённый впервые
#: запускает бота.
REF_COINS = max(0, _int("REF_COINS", "10"))

#: Сколько процентов от покупок приглашённого капает пригласившему.
#: Именно процент с покупок, а не только бонус за вход: платить за
#: голую регистрацию — это платить за пачки пустых аккаунтов.
REF_PERCENT = max(0, min(100, _int("REF_PERCENT", "10")))


# --- рассылка ---------------------------------------------------------

#: Как часто движок смотрит, кому пора писать. Не интервал рассылки —
#: просто шаг проверки очереди.
BROADCAST_TICK = max(1, _int("BROADCAST_TICK", "5"))

#: Минимальный интервал между сообщениями в одной рассылке, секунды.
#: Планка стоит намеренно: интервал в пять секунд — это не «быстрая
#: рассылка», это заявка на блокировку аккаунта в первый же час.
MIN_INTERVAL = max(10, _int("MIN_INTERVAL", "60"))

#: Минимальный разрыв между любыми двумя сообщениями одного аккаунта,
#: секунды. Отдельно от интервала рассылки, потому что рассылок у
#: аккаунта может быть несколько: три штуки по минуте — это для Telegram
#: один аккаунт, пишущий втрое чаще, и наказан будет он.
ACCOUNT_MIN_GAP = max(5, _int("ACCOUNT_MIN_GAP", "20"))

#: Сколько сообщений в сутки может отправить один аккаунт. Свежие
#: аккаунты Telegram проверяет строже — начинать лучше с малого и
#: поднимать постепенно.
DAILY_LIMIT = max(1, _int("DAILY_LIMIT", "200"))

#: Разброс интервала, доля. При 0.2 пауза гуляет ±20%: ровный ритм в
#: секунду секунду — самый заметный признак робота из всех.
INTERVAL_JITTER = _float("INTERVAL_JITTER", "0.2")

#: Сколько секунд держать подключение аккаунта живым после последней
#: отправки. Подключение занимает секунды, и переподключаться на каждое
#: сообщение — это и медленно, и лишние логины в глазах Telegram.
CLIENT_IDLE = max(60, _int("CLIENT_IDLE", "600"))

#: Потолок длины сообщения. У Telegram он 4096; оставляем запас под
#: подпись бесплатного тарифа.
MAX_TEXT = 3500

#: Сколько вариантов сообщения можно завести в одной рассылке.
MAX_VARIANTS = max(1, _int("MAX_VARIANTS", "10"))

#: Подписываться ли на чат, когда без подписки в него не пишут.
#: Во многих чатах стоит обязательная подписка на канал, и аккаунт,
#: который в нём не состоит, получает отказ на каждой отправке.
AUTO_JOIN = _bool("AUTO_JOIN", True)

#: Сколько вступлений в сутки на аккаунт. Вступления Telegram считает
#: отдельно от сообщений и наказывает за них так же — потолок здесь
#: заметно ниже, чем на сообщения, и это не осторожность ради
#: осторожности: пачка вступлений подряд у свежего аккаунта самый
#: короткий путь к ограничению.
JOIN_LIMIT = max(0, _int("JOIN_LIMIT", "20"))

#: Как долго не пробовать вступить в тот же чат после неудачи, секунды.
JOIN_RETRY_AFTER = max(600, _int("JOIN_RETRY_AFTER", "86400"))


def webapp_ready() -> bool:
    """Есть ли куда открывать приложение.

    Telegram кладёт в кнопку только https-адрес: http и localhost клиент
    молча не примет — кнопка будет, а нажатие ничего не даст.
    """
    return WEBAPP_URL.startswith("https://")


def mtproto_ready() -> bool:
    return bool(MTPROTO_API_ID and MTPROTO_API_HASH)


def api_hash_ok() -> bool:
    """Похож ли api_hash на настоящий: 32 знака шестнадцатеричных."""
    value = str(MTPROTO_API_HASH)
    return len(value) == 32 and all(c in "0123456789abcdefABCDEF" for c in value)


def check() -> None:
    """Что не так с настройками. Молчит, когда всё на месте."""
    if not BOT_TOKEN:
        raise SystemExit(
            "BOT_TOKEN пуст. Впишите в .env токен от @BotFather "
            "(шаблон — в .env.example)."
        )
    if not webapp_ready():
        log.warning(
            "WEBAPP_URL %s — кнопки «Открыть приложение» не будет. "
            "Telegram открывает мини-апп только по https; на своей "
            "машине поднимите туннель (cloudflared tunnel --url "
            "http://localhost:%s) и впишите его адрес.",
            f"= {WEBAPP_URL!r}" if WEBAPP_URL else "не задан",
            PORT,
        )
    if not mtproto_ready():
        log.warning(
            "MTPROTO_API_ID / MTPROTO_API_HASH не заданы — подключить "
            "аккаунт по номеру не выйдет. Ключи выдают бесплатно на "
            "my.telegram.org, раздел API development tools."
        )
    elif not api_hash_ok():
        # Самая частая ошибка заполнения — поменять их местами: id
        # числовой, hash из 32 знаков. Telegram на такое отвечает
        # «api_id/api_hash invalid» уже во время входа, и человек ищет
        # причину в номере телефона, а не в .env.
        log.warning(
            "MTPROTO_API_HASH не похож на ключ: ожидаются 32 знака "
            "(0-9, a-f), а пришло %s. Проверьте, не поменялись ли "
            "местами MTPROTO_API_ID и MTPROTO_API_HASH.",
            f"{len(MTPROTO_API_HASH)} знак(ов)",
        )
    if not SUPPORT_URL:
        log.warning(
            "SUPPORT_USERNAME не задан — кнопки «Поддержка» в меню не будет."
        )
    if not ADMIN_IDS:
        log.warning("ADMIN_IDS не заданы: /stats не откроется ни у кого")
    if not legal_ready():
        log.warning(
            "LEGAL_EMAIL и SUPPORT_USERNAME не заданы — в документах не "
            "будет контактов поддержки, а их банк требует отдельно."
        )
    if REVIEW_CODE:
        log.info(
            "кодовое слово проверяющего показывается на страницах "
            "документов и в боте: %s", REVIEW_CODE
        )
    if not platega_ready():
        log.warning(
            "PLATEGA_MERCHANT_ID / PLATEGA_SECRET не заданы — оплата "
            "рублями выключена, останется оплата звёздами."
        )


def session_key() -> bytes:
    """Ключ шифрования сессий: из окружения либо из файла рядом с базой.

    Файл создаётся один раз. Потерять его — то же, что потерять все
    подключённые аккаунты: расшифровать сессии будет нечем, и людям
    придётся входить по номеру заново.
    """
    if SESSION_KEY:
        return SESSION_KEY.encode()
    if SESSION_KEY_FILE.exists():
        saved = SESSION_KEY_FILE.read_bytes().strip()
        if saved:
            return saved

    from cryptography.fernet import Fernet

    key = Fernet.generate_key()
    SESSION_KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_KEY_FILE.write_bytes(key)
    try:
        SESSION_KEY_FILE.chmod(0o600)
    except OSError:
        # Windows про режимы файлов не знает — не повод падать.
        pass
    log.warning(
        "создан новый ключ шифрования сессий: %s. Не удаляйте его и не "
        "теряйте при переезде — иначе все подключённые аккаунты придётся "
        "подключать заново.",
        SESSION_KEY_FILE,
    )
    return key


def new_token() -> str:
    """Одноразовый идентификатор незавершённого входа."""
    return secrets.token_urlsafe(18)
