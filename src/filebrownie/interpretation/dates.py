"""Deterministic date parsing that keeps precision and ambiguity (D21, D34)."""

import calendar
import re
from dataclasses import dataclass
from datetime import date

from filebrownie.evidence.normalize import normalize


@dataclass(frozen=True)
class DateValue:
    """A possible calendar period for one reading; never an invented exact day."""

    start: date
    end: date
    precision: str  # day | month | year

    def as_json(self) -> dict:
        return {
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "precision": self.precision,
        }

    @classmethod
    def from_json(cls, data: dict) -> "DateValue":
        return cls(
            date.fromisoformat(data["start"]), date.fromisoformat(data["end"]), data["precision"]
        )


_EXACT_MONTHS = {
    "мая": 5,
    "май": 5,
    "травня": 5,
    "may": 5,
    "june": 6,
    "july": 7,
}
_MONTH_PREFIXES = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
    "oct": 10, "nov": 11, "dec": 12,
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
    "окт": 10, "ноя": 11, "дек": 12,
    "січ": 1, "лют": 2, "бер": 3, "кві": 4, "квіт": 4, "трав": 5, "черв": 6, "лип": 7,
    "серп": 8, "вер": 9, "жовт": 10, "лист": 11, "груд": 12,
}  # fmt: skip
_WORD = r"[^\W\d_]+"
_ISO = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_NUMERIC = re.compile(r"(?<!\d)(\d{1,2})([./-])(\d{1,2})\2(\d{4}|\d{2})(?!\d)")
_DAY_MONTH_YEAR = re.compile(rf"(?<!\d)(\d{{1,2}})(?:st|nd|rd|th)?\s+({_WORD})\.?,?\s+(\d{{4}})")
_MONTH_DAY_YEAR = re.compile(rf"({_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})")
_MONTH_YEAR = re.compile(rf"({_WORD})\.?\s+(\d{{4}})")
_NUMERIC_MONTH_YEAR = re.compile(r"(?<![\d./-])(\d{1,2})[./](\d{4})(?!\d)")
_YEAR_MONTH_DAY = re.compile(rf"(?<!\d)(\d{{4}})\s+({_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?(?!\d)")
_TRAILING_CLOCK = re.compile(r",\s*\d{1,2}:\d{2}(?::\d{2})?\s*$")
_YEAR = re.compile(r"^\D*(\d{4})\D*$")


def _month(word: str) -> int | None:
    if word in _EXACT_MONTHS:
        return _EXACT_MONTHS[word]
    for prefix in sorted(_MONTH_PREFIXES, key=len, reverse=True):
        if word.startswith(prefix) and len(word) <= len(prefix) + 6:
            return _MONTH_PREFIXES[prefix]
    return None


def _year(text: str) -> int:
    number = int(text)
    if len(text) == 4:
        return number
    return 2000 + number if number <= (date.today().year % 100) + 1 else 1900 + number


def _day(year: int, month: int, day: int) -> DateValue | None:
    if not (1900 <= year <= 2100 and 1 <= month <= 12):
        return None
    if not 1 <= day <= calendar.monthrange(year, month)[1]:
        return None
    exact = date(year, month, day)
    return DateValue(exact, exact, "day")


def _month_period(year: int, month: int) -> DateValue | None:
    if not (1900 <= year <= 2100 and 1 <= month <= 12):
        return None
    return DateValue(
        date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1]), "month"
    )


def _unique(values: list[DateValue | None]) -> tuple[DateValue, ...]:
    return tuple(dict.fromkeys(item for item in values if item is not None))


def parse_date(raw: str) -> tuple[DateValue, ...]:
    """Return every supported reading of `raw`; empty when it cannot be read as a date.

    A numeric day/month pair such as 03/04/2024 with a slash is ambiguous and yields both
    readings. Dotted dates are read day-first, as is usual in the source documents.
    """
    text = normalize(raw)
    text = _TRAILING_CLOCK.sub("", text.strip())
    if match := _ISO.search(text):
        return _unique([_day(int(match[1]), int(match[2]), int(match[3]))])
    if match := _NUMERIC.search(text):
        first, separator, second, year = int(match[1]), match[2], int(match[3]), _year(match[4])
        if separator in ".-":
            return _unique([_day(year, second, first)])
        if first > 12:
            return _unique([_day(year, second, first)])
        if second > 12:
            return _unique([_day(year, first, second)])
        return _unique([_day(year, second, first), _day(year, first, second)])
    if match := _DAY_MONTH_YEAR.search(text):
        month = _month(match[2])
        if month:
            return _unique([_day(int(match[3]), month, int(match[1]))])
    if match := _MONTH_DAY_YEAR.search(text):
        month = _month(match[1])
        if month:
            return _unique([_day(int(match[3]), month, int(match[2]))])
    if match := _YEAR_MONTH_DAY.search(text):
        month = _month(match[2])
        if month:
            return _unique([_day(int(match[1]), month, int(match[3]))])
    if match := _MONTH_YEAR.search(text):
        month = _month(match[1])
        if month:
            return _unique([_month_period(int(match[2]), month)])
    if match := _NUMERIC_MONTH_YEAR.search(text):
        return _unique([_month_period(int(match[2]), int(match[1]))])
    if match := _YEAR.match(text):
        year = int(match[1])
        if 1900 <= year <= 2100:
            return (DateValue(date(year, 1, 1), date(year, 12, 31), "year"),)
    return ()
