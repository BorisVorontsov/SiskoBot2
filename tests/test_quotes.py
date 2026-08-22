"""Тесты модели цитаты, очистки текста и подготовки сообщений Telegram."""

from __future__ import annotations

from datetime import datetime

from bot.quotes import (
    TG_MESSAGE_LIMIT,
    Quote,
    clean_quote_text,
    escape_html,
    extract_quote_id,
    format_messages,
    split_text,
    utf16_len,
)


def make_quote(text: str, **overrides) -> Quote:
    defaults = {
        "id": "470009",
        "text": text,
        "url": "https://башорг.рф/quote/470009",
        "published_at": datetime(2026, 8, 22, 9, 10),
        "guid": "",
    }
    defaults.update(overrides)
    return Quote(**defaults)


class TestCleanQuoteText:
    def test_br_becomes_newline(self):
        raw = "Жизнь до 30: интересно.&lt;br&gt;Жизнь после 30: понятно."
        assert clean_quote_text(raw) == "Жизнь до 30: интересно.\nЖизнь после 30: понятно."

    def test_double_escaped_entities_unwound(self):
        # В реальной ленте встречается &amp;gt; (двойное экранирование).
        assert clean_quote_text("xxx&amp;gt; привет") == "xxx> привет"

    def test_real_tags_removed(self):
        raw = "&lt;b&gt;жирный&lt;/b&gt; и &lt;a href='x'&gt;ссылка&lt;/a&gt;"
        assert clean_quote_text(raw) == "жирный и ссылка"

    def test_plain_lt_sign_preserved(self):
        text = "1<2 и x < y"
        assert clean_quote_text(text) == text

    def test_paragraphs_collapsed_but_kept(self):
        raw = "а&lt;br&gt;&lt;br&gt;&lt;br&gt;б"
        assert clean_quote_text(raw) == "а\n\nб"

    def test_trailing_whitespace_stripped(self):
        assert clean_quote_text("   привет   ") == "привет"


class TestExtractQuoteId:
    def test_valid_link(self):
        assert extract_quote_id("https://башорг.рф/quote/470009") == "470009"

    def test_relative_path(self):
        assert extract_quote_id("/quote/123") == "123"

    def test_foreign_url_is_none(self):
        assert extract_quote_id("https://example.com/page") is None

    def test_empty_is_none(self):
        assert extract_quote_id("") is None


class TestEscapeHtml:
    def test_escapes_specials(self):
        assert escape_html("a & b < c > d") == "a &amp; b &lt; c &gt; d"


class TestSplitText:
    def test_short_text_single_chunk(self):
        assert split_text("короткий", 100) == ["короткий"]

    def test_splits_at_paragraph_boundary(self):
        text = "а" * 60 + "\n\n" + "б" * 60
        chunks = split_text(text, 80)
        assert len(chunks) >= 2
        assert all(utf16_len(chunk) <= 80 for chunk in chunks)
        assert "".join(chunks).replace("\n", "") == "а" * 60 + "б" * 60

    def test_hard_split_of_giant_word(self):
        text = "ж" * 250
        chunks = split_text(text, 100)
        assert [utf16_len(c) for c in chunks] == [100, 100, 50]
        assert "".join(chunks) == text

    def test_zero_budget_raises(self):
        import pytest

        with pytest.raises(ValueError):
            split_text("текст", 0)

    def test_counts_utf16_units_not_codepoints(self):
        emoji = "😀"
        assert utf16_len(emoji) == 2
        # Один эмодзи «длиннее» лимита в единицах UTF-16.
        chunks = split_text(emoji * 5, 6)
        assert all(utf16_len(chunk) <= 6 for chunk in chunks)


class TestFormatMessages:
    def test_short_quote_single_message(self):
        quote = make_quote("Привет, мир!")
        messages = format_messages(quote)
        assert len(messages) == 1
        msg = messages[0]
        assert "<b>Цитата #470009</b>" in msg
        assert "22.08.2026 в 09:10" in msg
        assert "Привет, мир!" in msg
        assert '<a href="https://башорг.рф/quote/470009">оригинал</a>' in msg
        assert utf16_len(msg) <= TG_MESSAGE_LIMIT

    def test_body_html_escaped(self):
        quote = make_quote("<script>alert(1)</script> & кавычки")
        messages = format_messages(quote)
        assert "<script>" not in messages[0]
        assert "&lt;script&gt;" in messages[0]
        assert "&amp;" in messages[0]

    def test_long_quote_split_with_header_and_footer(self):
        body = "\n\n".join(f"абзац {i} " + "х" * 900 for i in range(8))
        quote = make_quote(body)
        messages = format_messages(quote)
        assert len(messages) > 1
        for message in messages:
            assert utf16_len(message) <= TG_MESSAGE_LIMIT
        # Заголовок только в первом, ссылка — только в последнем.
        assert "<b>Цитата #470009</b>" in messages[0]
        assert "<b>Цитата #470009</b>" not in messages[-1]
        assert "оригинал</a>" in messages[-1]
        assert "оригинал</a>" not in messages[0]
        # Ни один абзац не потерян.
        joined = "\n".join(messages)
        for i in range(8):
            assert f"абзац {i}" in joined

    def test_no_date_if_unknown(self):
        quote = make_quote("текст", published_at=None)
        messages = format_messages(quote)
        assert "в " not in messages[0].split("</b>")[1]

    def test_tiny_limit_rejected(self):
        import pytest

        with pytest.raises(ValueError):
            format_messages(make_quote("текст"), limit=300)
