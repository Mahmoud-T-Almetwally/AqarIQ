"""Unit tests for src.etl.arabic — hygiene rules on verbatim-style strings."""

from __future__ import annotations

from src.etl.arabic import (
    normalize_name_key,
    normalize_text,
    redact_noise,
    transliterate_digits,
    word_numerals_to_digits,
)


class TestDigits:
    def test_arabic_indic_to_ascii(self):
        assert transliterate_digits("١٢٣") == "123"

    def test_extended_arabic_indic_to_ascii(self):
        assert transliterate_digits("۴٥") == "45"

    def test_mixed(self):
        assert transliterate_digits("غرفة ١٠٠م") == "غرفة 100م"


class TestDiacriticsAndLetters:
    def test_tashkeel_stripped(self):
        # "شقة" with fatha/kasra diacritics
        assert normalize_text("شَقَّة فِي مدينَة") == "شقة في مدينة"

    def test_tatweel_stripped(self):
        assert normalize_text("شقـــة") == "شقة"

    def test_alef_variants_unified(self):
        assert normalize_text("أحمد إبراهيم آسر") == "احمد ابراهيم اسر"

    def test_alef_maksura_to_ya(self):
        assert normalize_text("على") == "علي"


class TestWordNumerals:
    def test_digit_plus_scale(self):
        assert word_numerals_to_digits("مقدم 2 مليون") == "مقدم 2000000"

    def test_word_number_plus_scale(self):
        assert word_numerals_to_digits("خمسة مليون جنيه") == "5000000 جنيه"

    def test_dual_scale(self):
        assert word_numerals_to_digits("مليونين جنيه") == "2000000 جنيه"

    def test_half_after_scale(self):
        assert word_numerals_to_digits("2 مليون ونص") == "2500000"

    def test_half_before_scale(self):
        assert word_numerals_to_digits("نص مليون") == "500000"

    def test_comma_numbers(self):
        assert word_numerals_to_digits("سعر 5,500,000 جنيه") == "سعر 5500000 جنيه"

    def test_untouched_when_no_numbers(self):
        assert word_numerals_to_digits("شقة مطلة على البحر") == "شقة مطلة على البحر"


class TestRedaction:
    def test_contact_placeholder(self):
        out = redact_noise("Contact : [contact details hidden] now")
        assert "contact details hidden" not in out
        assert "Contact" in out and "now" in out

    def test_hashtag(self):
        out = redact_noise("Great unit #Villas #October")
        assert "#Villas" not in out and "#October" not in out
        assert "Great unit" in out

    def test_phone(self):
        out = redact_noise("call 01012345678 today")
        assert "01012345678" not in out
        assert "call" in out and "today" in out

    def test_url(self):
        out = redact_noise("see https://example.com/xyz here")
        assert "example.com" not in out
        assert "see" in out and "here" in out


class TestNormalizeText:
    def test_full_pipeline(self):
        text = "شقة ١٠٠م #Cairo [contact details hidden]  متشطب"
        out = normalize_text(text)
        assert "100" in out
        assert "#" not in out
        assert "contact" not in out
        assert "متشطب" in out

    def test_none_stays_none(self):
        assert normalize_text(None) is None

    def test_whitespace_collapse(self):
        assert normalize_text("  Villa    for   sale  ") == "Villa for sale"


class TestNameKeys:
    def test_lower_and_collapse(self):
        assert normalize_name_key("  Stella   Di Mare 2 ") == "stella di mare 2"

    def test_none(self):
        assert normalize_name_key(None) is None
