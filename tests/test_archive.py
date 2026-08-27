"""Тесты выбора цитат из архива: поиск границы 2020 и случайная выборка."""

from __future__ import annotations

import random
from datetime import date, timedelta

import httpx

from bot.archive import (
    ARCHIVE_START_DATE,
    find_archive_start_id,
    pick_archive_quote,
)
from bot.quotes import SITE_TZ

SITE_BASE = "https://башорг.рф"
# Дата цитаты с ID=n равна DAYS_0 + n дней → граница 2020-01-01 на ID=12.
DAYS_0 = date(2019, 12, 20)


def render_quote_page(quote_id: int, date_text: str, body: str = "текст цитаты") -> str:
    return f"""<article class="quote" data-quote="{quote_id}">
            <div class="quote__frame">
                <header class="quote__header">
                    <a class="quote__header_permalink" href="/quote/{quote_id}">#{quote_id}</a>
                    <div class="quote__header_date">
                        {date_text}
                    </div>
                </header>
                <div class="quote__body">
                    {body}
                </div>
                <footer class="quote__footer">
                </footer>
            </div>
        </article>"""


def at_date(quote_id: int) -> date:
    return DAYS_0 + timedelta(days=quote_id)


def date_text(quote_id: int) -> str:
    d = at_date(quote_id)
    return f"{d:%d.%m.%Y в 12:00}"


# Каждая 5-я цитата «удалена»: сайт отвечает 200-страницей-заглушкой без даты.
STUB_PAGE = '<article class="quote" data-quote="0"><div class="quote__frame"></div></article>'


def _archive_handler(request: httpx.Request) -> httpx.Response:
    quote_id = int(request.url.path.rsplit("/", 1)[-1])
    if quote_id % 5 == 0:
        return httpx.Response(200, text=STUB_PAGE)
    return httpx.Response(200, text=render_quote_page(quote_id, date_text(quote_id)))


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestFindArchiveStartId:
    async def test_finds_first_quote_of_2020(self):
        async with _client(_archive_handler) as client:
            start_id = await find_archive_start_id(client, SITE_BASE, 100)
        assert start_id == 12
        assert at_date(start_id) == ARCHIVE_START_DATE

    async def test_skips_deleted_quotes_in_probe(self):
        # Граница попадает на удалённую цитату #10 — должно взять ближайшую живую.
        async with _client(_archive_handler) as client:
            start_id = await find_archive_start_id(client, SITE_BASE, 14)
        assert start_id == 12

    async def test_all_older_than_2020_returns_none(self):
        async with _client(_archive_handler) as client:
            start_id = await find_archive_start_id(client, SITE_BASE, 5)
        assert start_id is None

    async def test_everything_newer_returns_lo(self):
        async with _client(_archive_handler) as client:
            start_id = await find_archive_start_id(client, SITE_BASE, 100, start_id=20)
        assert start_id == 20


class TestPickArchiveQuote:
    def _app_rng(self, monkeypatch, target: int):
        rng = random.Random(7)
        monkeypatch.setattr(rng, "randint", lambda a, b: target)
        return rng

    async def test_returns_valid_quote(self, monkeypatch):
        async with _client(_archive_handler) as client:
            rng = self._app_rng(monkeypatch, 42)
            quote = await pick_archive_quote(client, SITE_BASE, 12, 100, set(), rng, attempts=3)
        assert quote is not None
        assert quote.id == "42"
        assert quote.published_at is not None
        assert quote.published_at.tzinfo is SITE_TZ

    async def test_skips_already_published(self, monkeypatch):
        async with _client(_archive_handler) as client:
            rng = self._app_rng(monkeypatch, 42)
            quote = await pick_archive_quote(client, SITE_BASE, 12, 100, {"42"}, rng, attempts=3)
        assert quote is None

    async def test_deleted_quote_skipped(self, monkeypatch):
        async with _client(_archive_handler) as client:
            rng = self._app_rng(monkeypatch, 50)  # 50 % 5 == 0 — удалена
            quote = await pick_archive_quote(client, SITE_BASE, 12, 100, set(), rng, attempts=3)
        assert quote is None

    async def test_before_2020_filtered_out(self, monkeypatch):
        async with _client(_archive_handler) as client:
            rng = self._app_rng(monkeypatch, 2)  # 2019-12-22
            quote = await pick_archive_quote(client, SITE_BASE, 12, 100, set(), rng, attempts=3)
        assert quote is None

    async def test_all_requests_404_returns_none(self):
        async with _client(lambda request: httpx.Response(404, text="not found")) as client:
            quote = await pick_archive_quote(
                client, SITE_BASE, 12, 100, set(), random.Random(1), attempts=3
            )
        assert quote is None


class TestArchiveStartDate:
    def test_boundary_is_2020_inclusive(self):
        assert date(2020, 1, 1) == ARCHIVE_START_DATE
