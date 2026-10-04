from datetime import date

import pytest

from filebrownie.evidence.normalize import contains_token, normalize
from filebrownie.interpretation.dates import parse_date


def test_normalize_folds_case_yo_apostrophes_decimals_and_spaces():
    assert normalize("  Гемоглобин   13,5 ") == "гемоглобин 13.5"
    assert normalize("Всё") == "все"
    assert normalize("Вiтамiн Д’3") == "вітамін д'3"
    assert normalize("Ferritin\u200b") == "ferritin"


def test_mixed_script_tokens_use_dominant_script_but_pure_tokens_stay():
    # A Latin 'o' inside a Cyrillic word, and a Cyrillic 'е' inside a Latin word.
    assert normalize("Гемоглoбин") == "гемоглобин"
    assert normalize("Hеmoglobin") == "hemoglobin"
    assert normalize("Гемоглобин") == "гемоглобин"
    assert normalize("Hemoglobin") == "hemoglobin"


def test_contains_token_requires_whole_tokens_and_whole_numbers():
    assert contains_token("гемоглобин 13.5 g/dl", "13.5")
    assert not contains_token("гемоглобин 113.5 g/dl", "13.5")
    assert not contains_token("ferritin 5.1 ng", "5")
    assert contains_token("ferritin: 5 ng", "5")
    assert not contains_token("abc", "")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("12.03.2024", [(date(2024, 3, 12), "day")]),
        ("2024-03-12", [(date(2024, 3, 12), "day")]),
        ("25/03/2024", [(date(2024, 3, 25), "day")]),
        ("03/25/2024", [(date(2024, 3, 25), "day")]),
        ("12 марта 2024", [(date(2024, 3, 12), "day")]),
        ("12 березня 2024 р.", [(date(2024, 3, 12), "day")]),
        ("March 12, 2024", [(date(2024, 3, 12), "day")]),
        ("12 March 2024", [(date(2024, 3, 12), "day")]),
        ("05.2024", [(date(2024, 5, 1), "month")]),
        ("травень 2024", [(date(2024, 5, 1), "month")]),
        ("2024", [(date(2024, 1, 1), "year")]),
    ],
)
def test_parse_date_keeps_precision(raw, expected):
    assert [(item.start, item.precision) for item in parse_date(raw)] == expected


def test_month_precision_covers_whole_calendar_month():
    (item,) = parse_date("02.2024")
    assert (item.start, item.end) == (date(2024, 2, 1), date(2024, 2, 29))


def test_slash_dates_with_both_parts_up_to_twelve_stay_ambiguous():
    values = parse_date("03/04/2024")
    assert {(item.start.month, item.start.day) for item in values} == {(4, 3), (3, 4)}
    assert len(parse_date("05/05/2024")) == 1


@pytest.mark.parametrize("raw", ["", "not a date", "31.02.2024", "13/13/2024", "Fe 5.1"])
def test_unparseable_dates_return_nothing(raw):
    assert parse_date(raw) == ()
