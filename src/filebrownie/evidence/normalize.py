"""Deterministic text normalization for matching. Original strings are always retained."""

import re
import unicodedata

# Visually identical Cyrillic <-> Latin letters. Mixed-script tokens are unified on their
# dominant script; pure tokens are never altered.
_CYRILLIC_TO_LATIN = str.maketrans("авекморстухі", "abekmopctyxi")
_LATIN_TO_CYRILLIC = str.maketrans("abekmopctyxi", "авекморстухі")
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff\u00ad"))
_APOSTROPHES = dict.fromkeys(map(ord, "\u2019\u2018\u02bc\u02b9\u0060\u00b4\u2032"), "'")
_TOKEN = re.compile(r"[^\W\d_]+(?:'[^\W\d_]+)*|\S", re.UNICODE)
_DECIMAL_COMMA = re.compile(r"(?<=\d),(?=\d)")
_SPACES = re.compile(r"\s+")


def _is_cyrillic(character: str) -> bool:
    return "\u0400" <= character <= "\u04ff"


def _is_latin(character: str) -> bool:
    return character.isalpha() and character.isascii()


def _unify_token(token: str) -> str:
    cyrillic = sum(_is_cyrillic(c) for c in token)
    latin = sum(_is_latin(c) for c in token)
    if not cyrillic or not latin:
        return token
    if cyrillic >= latin:
        return token.translate(_LATIN_TO_CYRILLIC)
    return token.translate(_CYRILLIC_TO_LATIN)


def normalize(text: str) -> str:
    """Fold case, Unicode forms, apostrophes, ё/е, homoglyphs, decimal commas, whitespace."""
    text = unicodedata.normalize("NFKC", text).translate(_INVISIBLE).translate(_APOSTROPHES)
    text = text.casefold().replace("ё", "е")
    text = _TOKEN.sub(lambda match: _unify_token(match.group()), text)
    text = _DECIMAL_COMMA.sub(".", text)
    return _SPACES.sub(" ", text).strip()


def contains_token(haystack: str, needle: str) -> bool:
    """Whole-token containment on already-normalized text (no partial numbers or words)."""
    if not needle:
        return False
    return re.search(rf"(?<![\w.]){re.escape(needle)}(?!\w|\.\d)", haystack) is not None


# Deterministic inflection tolerance for Russian/Ukrainian (and English plurals). Stems are
# for matching only; they are never displayed or stored as source text (D43).
_CYRILLIC_SUFFIXES = tuple(
    sorted(
        (
            "ами ями ого его ому ему ами ями ової евої ова ева ами ий ый ой ей ою ею ую юю ая яя "
            "ое ее ые ие ых их ым им ом ем ах ях ів ов ев ам ям ої ім ові еві ою "
            "у ю а я ы и і е є о ь й"
        ).split(),
        key=len,
        reverse=True,
    )
)
_WORD = re.compile(r"[\w']+(?:[-/.][\w']+)*", re.UNICODE)


def tokens(text: str) -> tuple[str, ...]:
    """Normalized word tokens, without surrounding punctuation."""
    return tuple(match.group() for match in _WORD.finditer(normalize(text)))


def stem(token: str) -> str:
    if len(token) >= 5 and token.isascii():
        if token.endswith("ies"):
            return token[:-3] + "y"
        if token.endswith("s") and not token.endswith("ss"):
            return token[:-1]
        return token
    if not any(_is_cyrillic(c) for c in token) or len(token) < 5:
        return token
    for suffix in _CYRILLIC_SUFFIXES:
        if token.endswith(suffix) and len(token) - len(suffix) >= 4:
            return token[: -len(suffix)]
    return token


def stems(text: str) -> tuple[str, ...]:
    return tuple(stem(token) for token in tokens(text))


def contains_phrase(haystack: tuple[str, ...], phrase: tuple[str, ...]) -> bool:
    """Consecutive-token containment of one token sequence in another."""
    size = len(phrase)
    if not size or size > len(haystack):
        return False
    return any(haystack[i : i + size] == phrase for i in range(len(haystack) - size + 1))
