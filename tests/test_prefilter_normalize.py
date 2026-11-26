import pytest

from deskpilot.prefilter import normalize, variants
from deskpilot.prefilter.normalize import MAX_INPUT_CHARS


def test_lowercase_yo_and_whitespace() -> None:
    assert normalize("  Всё   ЁЖИК \n\t тут ") == "все ежик тут"


def test_nfkc_folds_fullwidth_forms() -> None:
    assert normalize("ＤＥＬＥＴＥ") == "delete"


def test_zero_width_and_control_chars_are_removed() -> None:
    assert normalize("уда\u200bли\u200d \u202esystem\u000032") == "удали system32"


def test_punctuation_becomes_spaces() -> None:
    assert normalize(r"удали, пожалуйста: C:\Windows\System32!") == (
        "удали пожалуйста c windows system32"
    )


def test_long_input_is_truncated() -> None:
    out = normalize("a" * (MAX_INPUT_CHARS * 3))
    assert len(out) == MAX_INPUT_CHARS


def test_empty_input() -> None:
    assert normalize("") == ""
    assert variants("") == ("",)


def test_pure_and_deterministic() -> None:
    text = "Удали ВСЁ с диска C"
    assert normalize(text) == normalize(text)
    assert variants(text) == variants(text)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("udali vse fajly", "удали все файлы"),
        ("otklyuchi zashchitnik", "отключи защитник"),
        ("udali vse s diska c", "удали все с диска c"),
    ],
)
def test_transliteration_variant(text: str, expected: str) -> None:
    assert expected in variants(text)


def test_english_text_is_not_transliterated() -> None:
    assert variants("please delete the testing things") == ("please delete the testing things",)


def test_cyrillic_text_gets_no_transliteration() -> None:
    assert variants("удали все") == ("удали все",)


def test_homoglyphs_in_cyrillic_word_are_fixed() -> None:
    # Latin a, o, e inside Cyrillic words.
    assert variants("уд\u0061ли с\u006fтри")[-1] == "удали сотри"


def test_homoglyphs_in_latin_word_are_fixed() -> None:
    # Cyrillic е and о inside a Latin word.
    assert variants("d\u0435let\u0435 syst\u0435m32")[1] == "delete system32"


def test_separate_scripts_are_left_alone() -> None:
    assert variants("удали system32") == ("удали system32",)
