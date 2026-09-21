"""Нормализация текста и русская морфология.

Без этого модуля лексический поиск по русскому корпусу не работает: FTS5 с
токенайзером unicode61 корректно режет кириллицу и приводит регистр, но не знает
морфологии — "встреча", "встрече" и "встречу" остаются тремя разными токенами.

Здесь реализован стеммер Портера для русского (алгоритм Snowball) на чистом stdlib.
Он применяется симметрично: и при индексации, и при разборе запроса.
"""

from __future__ import annotations

import re

__all__ = [
    "normalize", "tokenize", "stem", "stem_tokens", "norm_text", "norm_filename",
    "STOPWORDS", "content_tokens",
]

# Служебные слова. Убираются ИЗ ЗАПРОСА, но остаются в индексе: в индексе они
# безвредны, а в запросе, объединённом через OR, каждый предлог тянет за собой
# десятки нерелевантных файлов.
#
# В список входит только «клей» — местоимения, предлоги, союзы, частицы.
# Слова, способные нести смысл в имени файла или заметке (два, три, один,
# больше, лучше, надо), сюда намеренно НЕ включены.
STOPWORDS = frozenset("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по
ее её мне вот от меня нет о из ему когда ну вдруг ли если или ни был были была
было быть него до вас вам ведь там потом себя ей они тут для мы тебя их чем сам
без чего себе под ж тогда кто этот этого этом эта эти эту это того тем том тот
та те ним ней них нее неё нас им мой моя мое моё твой ваш наш свой свою своего
при об над про через между перед после из-за из-под
""".split())


def content_tokens(text: str) -> list[str]:
    """Токены запроса без служебных слов и однобуквенных.

    Если после фильтрации не осталось ничего (запрос целиком состоит из
    служебных слов), возвращаются исходные токены — лучше шумный поиск,
    чем пустой.
    """
    toks = tokenize(text)
    kept = [t for t in toks if len(t) > 1 and t not in STOPWORDS]
    return kept or toks

_VOWELS = frozenset("аеиоуыэюя")
_CYRILLIC = re.compile(r"[а-яё]")
_TOKEN = re.compile(r"[0-9]+|[^\W\d_]+", re.UNICODE)

# Границы слова в именах файлов: разделители + переход регистра (ВстречаСКлиентом).
_FILENAME_SPLIT = re.compile(r"[\s_\-.,;()\[\]{}+#@!&'`~]+")
_CAMEL = re.compile(r"(?<=[a-zа-яё0-9])(?=[A-ZА-ЯЁ])")

# Группы окончаний. Внутри каждой группы порядок — от длинного к короткому,
# чтобы срабатывало самое длинное совпадение.
_PERFECTIVE_GERUND_1 = ("вшись", "вши", "в")           # требует предшествующей а/я
_PERFECTIVE_GERUND_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")

_ADJECTIVE = (
    "ими", "ыми", "его", "ого", "ему", "ому", "их", "ых",
    "ее", "ие", "ые", "ое", "ей", "ий", "ый", "ой",
    "ем", "им", "ым", "ом", "ую", "юю", "ая", "яя", "ою", "ею",
)
_PARTICIPLE_1 = ("ющ", "вш", "ем", "нн", "щ")           # требует предшествующей а/я
_PARTICIPLE_2 = ("ующ", "ивш", "ывш")

_REFLEXIVE = ("ся", "сь")

_VERB_1 = (
    "ешь", "нно", "ете", "йте", "ла", "на", "ли", "ем", "ло", "но",
    "ет", "ют", "ны", "ть", "й", "л", "н",
)                                                        # требует предшествующей а/я
_VERB_2 = (
    "ейте", "уйте", "ила", "ыла", "ена", "ите", "или", "ыли", "ило", "ыло",
    "ено", "ует", "уют", "ены", "ить", "ыть", "ишь", "ят", "ует",
    "ей", "уй", "ил", "ыл", "им", "ым", "ен", "ит", "ыт", "ую", "ю",
)
_NOUN = (
    "иями", "ями", "ами", "иях", "ией", "иям", "ием",
    "ях", "ах", "ов", "ев", "ие", "ье", "еи", "ии", "ей", "ой", "ий",
    "ям", "ем", "ам", "ом", "ию", "ью", "ия", "ья",
    "а", "е", "и", "й", "о", "у", "ы", "ь", "ю", "я",
)
_SUPERLATIVE = ("ейше", "ейш")
_DERIVATIONAL = ("ость", "ост")


def _sorted_desc(group: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(group, key=len, reverse=True))


_PERFECTIVE_GERUND_1 = _sorted_desc(_PERFECTIVE_GERUND_1)
_PERFECTIVE_GERUND_2 = _sorted_desc(_PERFECTIVE_GERUND_2)
_ADJECTIVE = _sorted_desc(_ADJECTIVE)
_PARTICIPLE_1 = _sorted_desc(_PARTICIPLE_1)
_PARTICIPLE_2 = _sorted_desc(_PARTICIPLE_2)
_VERB_1 = _sorted_desc(_VERB_1)
_VERB_2 = _sorted_desc(_VERB_2)
_NOUN = _sorted_desc(_NOUN)


def _regions(word: str) -> tuple[int, int, int]:
    """RV / R1 / R2 по определению Snowball.

    RV — позиция после первой гласной.
    R1 — позиция после первой согласной, идущей за гласной.
    R2 — то же самое, но внутри R1.
    """
    n = len(word)
    rv = n
    for i, ch in enumerate(word):
        if ch in _VOWELS:
            rv = i + 1
            break
    r1 = n
    for i in range(1, n):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r1 = i + 1
            break
    r2 = n
    for i in range(r1 + 1, n):
        if word[i] not in _VOWELS and word[i - 1] in _VOWELS:
            r2 = i + 1
            break
    return rv, r1, r2


def _strip(word: str, region: int, endings: tuple[str, ...],
           preceded_by: str | None = None) -> str | None:
    """Снять самое длинное подходящее окончание, если оно целиком лежит в region."""
    for end in endings:
        if not word.endswith(end):
            continue
        cut = len(word) - len(end)
        if cut < region:
            continue
        if preceded_by is not None:
            if cut == 0 or word[cut - 1] not in preceded_by:
                continue
            cut -= 1  # предшествующая а/я снимается вместе с окончанием
        return word[:cut]
    return None


def _adjectival(word: str, rv: int) -> str | None:
    base = _strip(word, rv, _ADJECTIVE)
    if base is None:
        return None
    trimmed = _strip(base, rv, _PARTICIPLE_2)
    if trimmed is None:
        trimmed = _strip(base, rv, _PARTICIPLE_1, preceded_by="ая")
    return trimmed if trimmed is not None else base


def stem(word: str) -> str:
    """Стем русского слова. Не-кириллические токены возвращаются как есть."""
    word = word.lower().replace("ё", "е")
    if not _CYRILLIC.search(word) or len(word) < 3:
        return word

    rv, _r1, r2 = _regions(word)

    # Шаг 1 — деепричастие / возвратность / прилагательное / глагол / существительное
    s = _strip(word, rv, _PERFECTIVE_GERUND_2)
    if s is None:
        s = _strip(word, rv, _PERFECTIVE_GERUND_1, preceded_by="ая")
    if s is not None:
        word = s
    else:
        refl = _strip(word, rv, _REFLEXIVE)
        if refl is not None:
            word = refl
        t = _adjectival(word, rv)
        if t is None:
            t = _strip(word, rv, _VERB_2)
        if t is None:
            t = _strip(word, rv, _VERB_1, preceded_by="ая")
        if t is None:
            t = _strip(word, rv, _NOUN)
        if t is not None:
            word = t

    # Шаг 2 — снять "и"
    if word.endswith("и") and len(word) - 1 >= rv:
        word = word[:-1]

    # Шаг 3 — словообразовательный суффикс в R2
    for end in _DERIVATIONAL:
        if word.endswith(end) and len(word) - len(end) >= r2:
            word = word[: -len(end)]
            break

    # Шаг 4 — удвоенное н / превосходная степень / мягкий знак
    if word.endswith("нн"):
        word = word[:-1]
    else:
        sup = _strip(word, rv, _SUPERLATIVE)
        if sup is not None:
            word = sup[:-1] if sup.endswith("нн") else sup
        elif word.endswith("ь") and len(word) - 1 >= rv:
            word = word[:-1]

    return word


def normalize(text: str) -> str:
    """Регистр вниз, ё → е. Применяется до токенизации."""
    return text.lower().replace("ё", "е")


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(normalize(text))


def stem_tokens(tokens: list[str]) -> list[str]:
    return [stem(t) for t in tokens]


def norm_text(text: str) -> str:
    """Текст → строка стеммированных токенов, пригодная для укладки в FTS5."""
    return " ".join(stem(t) for t in tokenize(text))


# Числовые штампы (дата съёмки, время экспорта, порядковый ID) длиннее этого
# порога выбрасываются из индексируемого имени. Год ("2024", "2026") и похожие
# короткие номера сохраняются — они реально бывают частью запроса; но
# "260401_150023" в "Проект_лого_260401_150023(1).pdf" — это метка времени,
# а не смысл, и как токен она только разбавляет совпадение со словами
# "проект"/"лого", отодвигая файл на 10-е место в собственном списке
# генератора. Найдено прогоном eval на реальном корпусе.
_LONG_NUMBER = re.compile(r"^\d{5,}$")


def norm_filename(name: str) -> str:
    """Имя файла → стеммированные токены.

    Имена вида ВстречаСКлиентом_2024-05 и meeting-notes.v2 режутся и по
    разделителям, и по смене регистра.
    """
    pieces: list[str] = []
    for raw in _FILENAME_SPLIT.split(name):
        if raw and not _LONG_NUMBER.match(raw):
            pieces.extend(p for p in _CAMEL.split(raw) if p)
    joined = " ".join(pieces)
    return norm_text(joined)
