"""Отбор заказов из потока чата: дешёвое сито, потом модель.

**Почему две ступени, а не одна.** В живом чате на заказчиков приходится
меньше процента сообщений — остальное болтовня, реакции, «+», споры о
ценах и реклама самих исполнителей. Гнать это через модель значит
платить за разбор слова «спасибо». Поэтому сначала работает предфильтр:
регулярки, ноль запросов, ноль денег. До модели доходит то, что похоже
на заказ хотя бы формой.

**Сито настроено на полноту, а не на точность** — и это осознанный
перекос. Пропущенный заказ стоит дороже, чем лишний запрос к модели:
запрос — это доли копейки, а заказ — это заказ. Поэтому предфильтр
отсекает только очевидное, а разбираться в смысле — работа второй
ступени.

**Главная ловушка этих чатов — не мусор, а зеркало.** Половина
сообщений в чате заказов написана другими исполнителями: «ищу работу»,
«возьму заказ», «делаю ботов, портфолио в личке». От настоящего заказа
это отличается одним словом, и ровно на этом ломается наивный поиск по
«ищу» и «нужен». Стоп-слова здесь ловят самые прямые формы саморекламы,
остальное отдаётся модели — потому что «ищу того, кто сделает» и «ищу,
что бы сделать» регуляркой не разводятся.

**Почему «портфолио» не в стоп-словах.** Просьба прислать портфолио —
признак настоящего заказчика не реже, чем реклама исполнителя. Слово,
которое одинаково часто встречается по обе стороны, в сите бесполезно и
только режет полноту.

**Пачками, но мелкими.** Системный промпт в запросе один и тот же,
и разбор по одному сообщению оплачивал бы его каждый раз. Складывать
помногу тоже нельзя: пачка ждёт, пока наберётся, а радар продаётся
скоростью ответа. Отсюда вилка `RADAR_BATCH` / `RADAR_BATCH_WAIT` —
что раньше наступит.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field

import config

log = logging.getLogger("rassylka.classify")


# --- предфильтр -------------------------------------------------------

#: Заказчик ищет исполнителя. Намеренно широко: это сито на полноту.
INTENT = re.compile(
    r"ищ[уеemм]\w*\s|ищем|нужен|нужна|нужно|нужны|требуе[тм]|разыскива|"
    r"кто\s+(?:может|возьм|сдела|напиш|поможет|знает)|"
    r"подскажите\s+кого|посоветуйте\s+кого|порекоменду|"
    r"есть\s+(?:задача|заказ|работа|проект)|"
    r"надо\s+(?:сделать|написать|запилить|доделать|починить)|"
    r"хочу\s+заказать|закажу|заказать\s+бот|"
    r"looking\s+for|need\s+a|hiring|wtb",
    re.IGNORECASE,
)

#: Разговор про деньги. Один этот признак заказом не делает, но заметно
#: поднимает шансы: в болтовне бюджеты не называют.
MONEY = re.compile(
    r"бюджет|оплат|оплач|заплач|гонорар|прайс|ценник|стоимост|"
    r"\d\s*(?:000|к\b|тыс|т\.?р\b|руб|₽|\$|usd|eur|€)|"
    r"за\s+работу|по\s+факту|предоплат|budget|paid",
    re.IGNORECASE,
)

#: Исполнитель рекламирует себя. Только самые прямые формы: всё, что
#: читается двояко, должна решать модель, а не регулярка.
MIRROR = re.compile(
    r"ищу\s+(?:работу|заказ|проект|подработ|клиент)|"
    r"в\s+поиск\w*\s+(?:работы|заказов|проектов)|"
    r"возьм[уё]\s+(?:заказ|проект|работу)|возьмусь\s+за|"
    r"готов\w*\s+(?:взять|взяться|поработать|выполнить)|"
    r"сво?боден\s+для|есть\s+свободн\w+\s+(?:время|руки|слот)|"
    r"(?:мо[йяё]|наш[аеи]?)\s+(?:опыт|работы|кейс|портфолио|резюме)|"
    r"оказыва[юе]м?\s+услуг|предлага[юе]м?\s+услуг|"
    r"открыт\w*\s+к\s+предложениям|рассмотрю\s+предложения|"
    r"о\s+себе\s*:|резюме\s*:|"
    r"(?:дела[юе]м?|пиш[уе]м?|разрабатыва[юе]м?)\s+(?:бот|сайт|прилож)",
    re.IGNORECASE,
)

#: Заведомый шум: пересланная реклама, ссылки без слов, стикеры.
NOISE = re.compile(
    r"^\s*(?:https?://\S+\s*)+$|^\s*[@#]\w+\s*$|подписывайся|подпишись|"
    r"реклама\s*:|розыгрыш|раздача\s+подарков",
    re.IGNORECASE,
)


@dataclass(slots=True)
class Candidate:
    """Сообщение, дошедшее до модели."""

    chat_id: int
    chat_title: str
    msg_id: int
    author_id: int
    author_name: str
    author_user: str | None
    text: str


@dataclass(slots=True)
class Verdict:
    """Разбор одного сообщения."""

    order: bool
    score: int = 0
    budget: int = 0
    currency: str = ""
    urgency: str = ""
    stack: list[str] = field(default_factory=list)
    summary: str = ""
    #: Разбирала ли это модель. False — вердикт собран регулярками, и
    #: доверия ему заметно меньше: карточка об этом честно пишет.
    judged: bool = True


def prefilter(text: str, keywords: str = "") -> bool:
    """Похоже ли сообщение на заказ настолько, чтобы платить за разбор.

    Порядок проверок важен: зеркало («ищу работу») отсекается до того,
    как сработает намерение («ищу»), иначе реклама исполнителей прошла
    бы сито целиком — она написана теми же словами, что и заказ.
    """
    body = (text or "").strip()
    if len(body) > config.RADAR_MAX_LEN or NOISE.search(body):
        return False
    if MIRROR.search(body):
        return False

    wants = bool(INTENT.search(body))
    pays = bool(MONEY.search(body))
    if not (wants or pays):
        return False

    # Нижняя граница длины зависит от силы сигнала. «Нужен бот для
    # рассылки, бюджет 20к» — это тридцать символов и совершенно
    # настоящий заказ: намерение и деньги вместе говорят достаточно,
    # чтобы не требовать объёма. Одного признака мало — короткое «кто
    # может помочь» без денег значит что угодно, и общий порог там
    # остаётся.
    floor = config.RADAR_MIN_LEN // 2 if (wants and pays) else config.RADAR_MIN_LEN
    if len(body) < floor:
        return False

    words = [w.strip().lower() for w in keywords.split(",") if w.strip()]
    if words:
        low = body.lower()
        return any(word in low for word in words)
    return True


def fingerprint(author_id: int, text: str) -> str:
    """Отпечаток заказа для склейки повторов.

    Один и тот же заказ, разосланный по десяти чатам, приходит десятью
    сообщениями с одинаковым телом. Нормализуем текст до костей —
    регистр, пробелы, пунктуация, эмодзи, — и берём от него хэш вместе
    с автором: разные люди с похожим текстом остаются разными лидами.

    Длинный хвост в отпечаток не берётся: к концу объявления люди чаще
    всего дописывают, куда писать, и в каждом чате по-своему.
    """
    bones = re.sub(r"[^\w\s]", "", (text or "").lower(), flags=re.UNICODE)
    bones = re.sub(r"\s+", " ", bones).strip()[:200]
    raw = f"{author_id}:{bones}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# --- разбор без модели ------------------------------------------------

#: Вилка: «от 20 до 50 тысяч», «15-20к», «30 000 — 40 000 руб». Ищется
#: первой, потому что по отдельности её половинки выглядят как два
#: независимых бюджета, и осмысленного выбора между ними уже не сделать.
BUDGET_RANGE = re.compile(
    r"(?:от\s*)?(\d[\d\s.,]{0,9}?)\s*(?:-|—|–|до)\s*(\d[\d\s.,]{0,9})\s*"
    r"(к\b|k\b|тыс\w*|т\.?р\b|руб\w*|₽|\$|usd|eur|€)",
    re.IGNORECASE,
)

#: Число с денежной пометкой позади: «30к», «15 000 руб», «50 тыс».
#: Число берётся жадно — «15 000» это пятнадцать тысяч, а не
#: пятнадцать: пробел внутри числа тут разряд, а не конец числа.
BUDGET_TAGGED = re.compile(
    r"(\d[\d\s.,]{0,9})\s*(к\b|k\b|тыс\w*|т\.?р\b|руб\w*|₽|\$|usd|eur|€)",
    re.IGNORECASE,
)

#: Валюта впереди числа: «$500», «€1200». Отдельным выражением, потому
#: что знак слева от числа — это другой порядок, и втиснуть его в то же
#: выражение можно только ценой нечитаемости.
BUDGET_LEADING = re.compile(
    r"([$€]|usd|eur)\s*(\d[\d\s.,]{0,9})",
    re.IGNORECASE,
)

#: Голое число сразу после разговора о деньгах: «бюджет 45000».
#: Без привязки к такому слову в бюджет уехало бы любое число из
#: текста — счёт пользователей, срок в днях, версия библиотеки.
BUDGET_NEAR = re.compile(
    r"(?:бюджет|оплат\w*|заплач\w*|цена|ценник|стоимость|гонорар)\D{0,12}"
    r"(\d[\d\s.,]{0,9})",
    re.IGNORECASE,
)

#: Во сколько раз пометка увеличивает число.
SCALE = {"к": 1000, "k": 1000, "тыс": 1000, "т.р": 1000, "тр": 1000}

#: Какой валютой считать пометку. Всё неназванное — рубли: чаты
#: русскоязычные, и это верно в подавляющем большинстве случаев.
MONEY_OF = {"$": "USD", "usd": "USD", "€": "EUR", "eur": "EUR"}


def _number(raw: str) -> int:
    """Число из куска текста. Пробелы и точки внутри — разряды."""
    digits = re.sub(r"[^\d]", "", raw or "")
    return int(digits) if digits and len(digits) <= 9 else 0


def _tag(mark: str) -> tuple[int, str]:
    low = (mark or "").lower().rstrip(".")
    scale = next((v for k, v in SCALE.items() if low.startswith(k)), 1)
    return scale, MONEY_OF.get(low, "RUB")


def budget_of(text: str) -> tuple[int, str]:
    """Бюджет и валюта из текста регулярками, без модели.

    Вилки сводятся к нижней границе, а из нескольких найденных чисел
    берётся наименьшее. Обе оговорки про одно и то же: завышенный
    бюджет врёт опаснее заниженного — по нему человек отсеет заказ,
    который на самом деле проходил по деньгам, и даже не узнает об этом.

    Ноль значит «не назван», а не «бесплатно», и радар относится к нему
    именно так: заказ без цифры показывается, просто без строки о
    деньгах.
    """
    body = text or ""

    found: list[tuple[int, str]] = []
    for low, _high, mark in BUDGET_RANGE.findall(body):
        scale, money = _tag(mark)
        value = _number(low)
        if value:
            found.append((value * scale, money))

    if not found:
        for raw, mark in BUDGET_TAGGED.findall(body):
            scale, money = _tag(mark)
            value = _number(raw)
            if value:
                found.append((value * scale, money))
        for mark, raw in BUDGET_LEADING.findall(body):
            value = _number(raw)
            if value:
                found.append((value, _tag(mark)[1]))

    if not found:
        for raw in BUDGET_NEAR.findall(body):
            value = _number(raw)
            if value:
                found.append((value, "RUB"))

    if not found:
        return 0, ""

    # Мелочь рядом с деньгами — это чаще проценты, часы или количество
    # правок, чем бюджет. Отбрасываем её, но только если осталось что-то
    # ещё: заказ на пятьсот рублей тоже заказ.
    real = [pair for pair in found if pair[0] >= 500] or found
    return min(real, key=lambda pair: pair[0])


def _offline(items: list[Candidate]) -> list[Verdict]:
    """Вердикты без модели: всё, что прошло сито, считается заказом.

    Это сознательный перекос в другую сторону. Модель отделяет заказ от
    похожего на заказ; без неё такого разделения нет вообще, и выбор
    стоит между «показать лишнее» и «не показать ничего». Второе
    выглядит как сломанный радар, поэтому показываем — с честной
    пометкой в карточке, что смысл никто не разбирал.

    Оценка тут не суждение, а сила сигнала: названные деньги и объём
    описания — единственное, что регулярки про сообщение знают.
    """
    out: list[Verdict] = []
    for item in items:
        text = item.text.strip()
        budget, money = budget_of(text)

        score = 55
        if MONEY.search(text):
            score += 10
        if budget:
            score += 10
        if len(text) >= 200:
            score += 5

        # Первая фраза вместо пересказа: сокращать смысл регулярками
        # некому, а первая строка объявления почти всегда и есть суть.
        head = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
        out.append(
            Verdict(
                order=True,
                score=min(80, score),
                budget=budget,
                currency=money,
                urgency="high" if re.search(r"срочн|асап|asap|сегодня", text,
                                            re.IGNORECASE) else "",
                stack=[],
                summary=head[:100],
                judged=False,
            )
        )
    return out


# --- модель -----------------------------------------------------------

SYSTEM = """Ты отбираешь заказы для фрилансера из потока сообщений \
телеграм-чатов.

На каждое сообщение реши: это заказ, который автор готов отдать \
исполнителю за деньги, — или нет.

Заказ — это когда автор ищет, кому поручить работу.
Не заказ — всё остальное, и особенно:
- исполнитель рекламирует себя или ищет работу («делаю ботов», «возьму \
заказ», «мой опыт»);
- обсуждение, спор, вопрос по технологии, просьба совета;
- вакансия в штат с трудоустройством, если человек ищет разовые заказы;
- перепродажа чужого заказа без деталей;
- предложение купить готовый продукт, курс, подписку.

Оценка score 0..100 — насколько этот заказ стоит внимания именно этого \
исполнителя. Учитывай совпадение с его профилем, внятность задачи, \
названный бюджет и признаки платёжеспособности. Расплывчатое «нужен \
бот, пишите в лс» без деталей — это 30-40, а не 80. Если профиль \
исполнителя не совпадает с задачей, score низкий, даже когда заказ \
настоящий.

budget — число в валюте currency, если названо; иначе 0. Диапазон \
округляй к нижней границе. «15к» — это 15000.
urgency — one of: low, normal, high.
stack — технологии из задачи, до пяти штук, пустой список если не \
названы.
summary — одна строка по-русски, до 100 символов, суть задачи без \
вводных.

Отвечай на каждое сообщение по его номеру n. Не пропускай номера."""

RESULT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "order": {"type": "boolean"},
                    "score": {"type": "integer"},
                    "budget": {"type": "integer"},
                    "currency": {"type": "string"},
                    "urgency": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                    },
                    "stack": {"type": "array", "items": {"type": "string"}},
                    "summary": {"type": "string"},
                },
                "required": [
                    "n", "order", "score", "budget", "currency",
                    "urgency", "stack", "summary",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

_client = None
#: Трёхзначное состояние готовности: None — ещё не проверяли. Проверка
#: кэшируется, потому что ready() зовётся на каждой пачке, а жаловаться
#: на отсутствие пакета в лог по сто раз в час незачем.
_usable: bool | None = None


def ready() -> bool:
    """Есть ли чем разбирать смысл.

    Ни без ключа, ни без пакета радар не падает — он работает одним
    предфильтром. Лента получается заметно мусорнее, но лучше мусорная
    лента, чем радар, который не поднялся: сбор сообщений и отметки
    прочитанного от классификатора не зависят.
    """
    global _usable
    if _usable is not None:
        return _usable

    if not config.ANTHROPIC_API_KEY:
        log.warning(
            "ANTHROPIC_API_KEY не задан: радар работает одним предфильтром"
        )
        _usable = False
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        log.warning(
            "пакет anthropic не установлен (pip install -r requirements.txt): "
            "радар работает одним предфильтром"
        )
        _usable = False
        return False

    _usable = True
    return True


def _api():
    global _client
    if _client is None:
        from anthropic import AsyncAnthropic

        _client = AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


async def close() -> None:
    global _client
    if _client is not None:
        try:
            await _client.close()
        except Exception as error:
            log.debug("клиент Anthropic не закрылся: %s", error)
        _client = None


def _payload(profile: str, items: list[Candidate]) -> str:
    lines = [
        "Профиль исполнителя:",
        profile.strip() or "(не указан — оценивай только внятность заказа)",
        "",
        "Сообщения:",
    ]
    for number, item in enumerate(items, 1):
        # Чат в разборе участвует: одна и та же фраза в чате по ботам и
        # в чате по дизайну значит разное.
        lines.append(f"[{number}] чат «{item.chat_title}»")
        lines.append(item.text.strip()[: config.RADAR_MAX_LEN])
        lines.append("")
    return "\n".join(lines)


async def classify(profile: str, items: list[Candidate]) -> list[Verdict]:
    """Разобрать пачку. Длина ответа равна длине запроса.

    При любом отказе модели возвращаются пустые вердикты, а не
    исключение: сорванный разбор одной пачки не должен ронять радар —
    следующие сообщения разберутся, а эти потеряются, и это меньшее зло
    по сравнению с вставшим слушателем.
    """
    if not items:
        return []
    if not ready():
        return _offline(items)

    import anthropic

    try:
        response = await _api().messages.create(
            model=config.RADAR_MODEL,
            # Вердикт — это сотня токенов JSON. Запас на случай, если
            # модель распишет summary щедрее ожидаемого.
            max_tokens=600 + 220 * len(items),
            system=[
                {
                    "type": "text",
                    "text": SYSTEM,
                    # Блок общий для всех тенантов и не меняется между
                    # запросами — единственное место здесь, где кэш
                    # вообще может сработать.
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            output_config={
                # Классификация — не та задача, где нужна глубина: на
                # высоком усилии это просто дороже при том же ответе.
                "effort": "low",
                "format": {"type": "json_schema", "schema": RESULT_SCHEMA},
            },
            messages=[{"role": "user", "content": _payload(profile, items)}],
        )
    except anthropic.RateLimitError as error:
        log.warning("классификатор упёрся в лимит: %s", error)
        return [Verdict(order=False) for _ in items]
    except anthropic.APIStatusError as error:
        log.warning("классификатор отказал (%s): %s", error.status_code, error)
        return [Verdict(order=False) for _ in items]
    except anthropic.APIConnectionError as error:
        log.warning("классификатор недоступен: %s", error)
        return [Verdict(order=False) for _ in items]

    return _unpack(response, len(items))


def _unpack(response, count: int) -> list[Verdict]:
    """Разложить ответ по местам.

    Модель отвечает номерами, а не порядком, и полагаться на порядок
    нельзя: один пропущенный номер сдвинул бы все вердикты на единицу,
    и заказ уехал бы на чужое сообщение — с чужим чатом и чужим
    автором в карточке.
    """
    out = [Verdict(order=False) for _ in range(count)]
    if getattr(response, "stop_reason", None) == "refusal":
        log.warning("классификатор отклонил пачку целиком")
        return out

    text = next(
        (block.text for block in response.content if block.type == "text"), ""
    )
    try:
        data = json.loads(text)
    except ValueError:
        log.warning("классификатор вернул не JSON: %.200s", text)
        return out

    for row in data.get("verdicts") or []:
        try:
            index = int(row.get("n", 0)) - 1
        except (TypeError, ValueError):
            continue
        if not 0 <= index < count:
            continue
        stack = row.get("stack")
        out[index] = Verdict(
            order=bool(row.get("order")),
            score=max(0, min(100, int(row.get("score") or 0))),
            budget=max(0, int(row.get("budget") or 0)),
            currency=(row.get("currency") or "").strip()[:8],
            urgency=(row.get("urgency") or "").strip()[:8],
            stack=[str(s)[:24] for s in stack][:5] if isinstance(stack, list) else [],
            summary=(row.get("summary") or "").strip()[:200],
        )
    return out
