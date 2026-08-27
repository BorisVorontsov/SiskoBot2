"""Тесты разбора RSS-ленты и получения её по HTTP."""

from __future__ import annotations

import httpx
import pytest

from bot.feed import FeedError, fetch_feed, fetch_quote_page, parse_feed

SITE_BASE = "https://башорг.рф"

DESC_470009 = "Жизнь до 30: ничего не понятно.&lt;br&gt;Жизнь после 30: понятно."

RSS_SAMPLE = f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"
     xmlns:atom="http://www.w3.org/2005/Atom"
     xmlns:dc="http://purl.org/dc/elements/1.1/"
     xmlns:content="http://purl.org/rss/1.0/modules/content/">
    <channel>
        <title>Башорг.рф</title>
        <link>{SITE_BASE}/</link>
        <atom:link href="{SITE_BASE}/rss/" rel="self" type="application/rss+xml" />
        <description>Цитатник Рунета</description>
        <language>ru</language>
        <item>
            <guid isPermaLink="false">aaa111</guid>
            <link>{SITE_BASE}/quote/470009</link>
            <title>Цитата #470009</title>
            <pubDate>Sat, 22 Aug 2026 09:10:01 +0300</pubDate>
            <description><![CDATA[{DESC_470009}]]></description>
        </item>
        <item>
            <guid isPermaLink="false">bbb222</guid>
            <link>{SITE_BASE}/quote/470008</link>
            <title>Цитата #470008</title>
            <pubDate>Fri, 21 Aug 2026 09:01:01 +0300</pubDate>
            <description><![CDATA[Цитата с &amp;gt; двойным экранированием]]></description>
        </item>
        <item>
            <guid isPermaLink="false">ccc333</guid>
            <link>{SITE_BASE}/quote/470007</link>
            <title>Цитата #470007</title>
            <pubDate>мусор вместо даты</pubDate>
            <description><![CDATA[Без валидной даты]]></description>
        </item>
        <item>
            <guid isPermaLink="false">ddd444</guid>
            <title>Пустая цитата без описания</title>
        </item>
    </channel>
</rss>
"""


class TestParseFeed:
    def test_parses_all_valid_items(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert [q.id for q in quotes] == ["470009", "470008", "470007"]

    def test_id_extracted_from_link(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert quotes[0].id == "470009"

    def test_text_cleaned(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert quotes[0].text == "Жизнь до 30: ничего не понятно.\nЖизнь после 30: понятно."

    def test_double_escaped_unescaped(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert ">" in quotes[1].text
        assert "&gt;" not in quotes[1].text

    def test_pubdate_parsed(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        published = quotes[0].published_at
        assert published is not None
        assert (published.year, published.month, published.day) == (2026, 8, 22)
        # Смещение из ленты сохранено (+03:00), время — как на сайте.
        assert published.hour == 9 and published.utcoffset() is not None

    def test_bad_date_does_not_break_item(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert quotes[2].published_at is None
        assert quotes[2].text == "Без валидной даты"

    def test_item_without_description_skipped(self):
        quotes = parse_feed(RSS_SAMPLE, SITE_BASE)
        assert "ddd444" not in {q.id for q in quotes}

    def test_broken_xml_raises(self):
        with pytest.raises(FeedError):
            parse_feed("<rss><channel><item></rss>", SITE_BASE)

    def test_html_page_instead_of_rss_raises(self):
        page = "<!doctype html><html><body><h1>403 Forbidden</h1></body></html>"
        with pytest.raises(FeedError):
            parse_feed(page, SITE_BASE)

    def test_rss_without_channel_raises(self):
        with pytest.raises(FeedError):
            parse_feed('<?xml version="1.0"?><rss version="2.0"></rss>', SITE_BASE)

    def test_empty_channel_ok(self):
        rss = '<?xml version="1.0"?><rss version="2.0"><channel><title>x</title></channel></rss>'
        assert parse_feed(rss, SITE_BASE) == []


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFetchFeed:
    async def test_returns_body_on_200(self):
        async with _client(lambda request: httpx.Response(200, text=RSS_SAMPLE)) as client:
            body = await fetch_feed(client, f"{SITE_BASE}/rss/")
        assert "470009" in body

    async def test_http_error_becomes_feed_error(self):
        async with _client(lambda request: httpx.Response(503, text="unavailable")) as client:
            with pytest.raises(FeedError, match="HTTP 503"):
                await fetch_feed(client, f"{SITE_BASE}/rss/")

    async def test_timeout_becomes_feed_error(self):
        def handler(request):
            raise httpx.ConnectTimeout("boom", request=request)

        async with _client(handler) as client:
            with pytest.raises(FeedError, match="Сетевая ошибка"):
                await fetch_feed(client, f"{SITE_BASE}/rss/")

    async def test_garbage_response_fails_at_parse_stage(self):
        """Некорректный ответ сайта не должен приводить к тихому успеху."""
        async with _client(lambda request: httpx.Response(200, text="\xff\xfe garbage")) as client:
            body = await fetch_feed(client, f"{SITE_BASE}/rss/")
        with pytest.raises(FeedError):
            parse_feed(body, SITE_BASE)


class TestFetchQuotePage:
    """Страница отдельной цитаты: 404 — «удалённая» цитата, не ошибка."""

    QUOTE_URL = f"{SITE_BASE}/quote/470009"

    async def test_returns_body_on_200(self):
        async with _client(lambda request: httpx.Response(200, text="<article>…")) as client:
            assert await fetch_quote_page(client, self.QUOTE_URL) == "<article>…"

    async def test_404_becomes_none(self):
        async with _client(lambda request: httpx.Response(404, text="nope")) as client:
            assert await fetch_quote_page(client, self.QUOTE_URL) is None

    async def test_server_error_raises(self):
        async with _client(lambda request: httpx.Response(503, text="busy")) as client:
            with pytest.raises(FeedError, match="HTTP 503"):
                await fetch_quote_page(client, self.QUOTE_URL)

    async def test_network_error_raises(self):
        def handler(request):
            raise httpx.ReadTimeout("boom", request=request)

        async with _client(handler) as client:
            with pytest.raises(FeedError, match="Сетевая ошибка"):
                await fetch_quote_page(client, self.QUOTE_URL)
