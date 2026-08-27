"""Тесты команд /help и /next, паузы с прослушиванием и переноса между группами."""

from __future__ import annotations

import time
from datetime import timedelta
from types import SimpleNamespace

import httpx

from bot.app import (
    HELP_TEXT,
    PAUSE_EXPIRED,
    PAUSE_KICKED,
    PAUSE_NEXT,
    App,
    parse_command,
)
from bot.config import Config

TOKEN = "1234567890:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKKlll"
GROUP_ID = -1001234567890


def make_update(
    text: str | None,
    update_id: int = 1,
    chat_id: int = GROUP_ID,
    chat_type: str = "supergroup",
):
    if text is None:
        message = None
    else:
        message = SimpleNamespace(text=text, chat=SimpleNamespace(id=chat_id, type=chat_type))
    return SimpleNamespace(update_id=update_id, message=message, my_chat_member=None)


def make_member_update(chat_id: int, status: str, update_id: int = 1):
    my_chat_member = SimpleNamespace(
        chat=SimpleNamespace(id=chat_id, type="supergroup"),
        new_chat_member=SimpleNamespace(status=status),
    )
    return SimpleNamespace(update_id=update_id, message=None, my_chat_member=my_chat_member)


def make_app(tmp_path) -> App:
    cfg = Config.load({"TELEGRAM_BOT_TOKEN": TOKEN, "DB_PATH": str(tmp_path / "state.db")})
    return App(cfg)


class FakeClient:
    """Отдаёт заготовленные батчи обновлений без сети и запоминает отправки."""

    def __init__(self, batches, discovered_chat_id: int = -100999000111):
        self._batches = list(batches)
        self.discovered_chat_id = discovered_chat_id
        self.poll_timeouts: list[float | None] = []
        self.flushed = False
        self.sent: list[str] = []
        self.poll_timeout = 25

    async def poll_updates(self, timeout=None):
        self.poll_timeouts.append(timeout)
        if not self._batches:
            return ()
        return tuple(self._batches.pop(0))

    async def flush_pending_updates(self, max_batches: int = 40) -> None:
        self.flushed = True

    async def send_messages(self, chat_id, texts):
        self.sent.extend(texts)

    async def discover_chat_id(self) -> int:
        return self.discovered_chat_id


class TestParseCommand:
    def test_plain_next(self):
        assert parse_command("/next") == ("next", "")

    def test_bot_suffix_stripped(self):
        assert parse_command("/next@BashQuotesBot") == ("next", "")

    def test_argument_preserved(self):
        assert parse_command("/next@BashQuotesBot скорее") == ("next", "скорее")

    def test_case_insensitive(self):
        assert parse_command("/HELP") == ("help", "")

    def test_not_a_command(self):
        assert parse_command("привет /next") == (None, "")

    def test_empty_text(self):
        assert parse_command("") == (None, "")

    def test_none_text(self):
        assert parse_command(None) == (None, "")

    def test_unknown_command_parsed_but_named(self):
        assert parse_command("/configtime 2, 6") == ("configtime", "2, 6")


class TestCommandAction:
    def test_help_detected_in_group(self):
        assert App._command_action(make_update("/help")) == ("help", -1001234567890)

    def test_next_with_suffix_detected(self):
        assert App._command_action(make_update("/next@SomeBot")) == ("next", -1001234567890)

    def test_next_in_private_chat_detected(self):
        update = make_update("/next", chat_id=42, chat_type="private")
        assert App._command_action(update) == ("next", 42)

    def test_plain_message_ignored(self):
        assert App._command_action(make_update("всем привет")) == (None, None)

    def test_unknown_command_ignored(self):
        assert App._command_action(make_update("/start")) == (None, None)

    def test_update_without_message_ignored(self):
        assert App._command_action(SimpleNamespace(update_id=1, message=None)) == (None, None)


class TestPauseAndListen:
    async def test_help_answers_and_keeps_waiting(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([[make_update("/help")]])
            outcome = await app._pause_and_listen(client, GROUP_ID, timedelta(seconds=0.2))
            assert client.sent == [HELP_TEXT]
            assert outcome == PAUSE_EXPIRED
        finally:
            app.close()

    async def test_next_returns_before_deadline(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([[make_update("/next@MyBot", update_id=7)]])
            started = time.monotonic()
            outcome = await app._pause_and_listen(client, -100, timedelta(seconds=60))
            elapsed = time.monotonic() - started
            assert elapsed < 5
            assert outcome == PAUSE_NEXT
            assert client.sent == []
        finally:
            app.close()

    async def test_next_from_private_chat_cuts_pause(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([[make_update("/next", update_id=3, chat_id=42)]])
            outcome = await app._pause_and_listen(client, -100, timedelta(seconds=60))
            assert outcome == PAUSE_NEXT
        finally:
            app.close()

    async def test_help_from_private_chat_replies_to_private(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([[make_update("/help", chat_id=42, chat_type="private")]])
            await app._pause_and_listen(client, -100, timedelta(seconds=0.2))
            assert client.sent == [HELP_TEXT]
        finally:
            app.close()

    async def test_silence_polls_until_deadline(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([])
            outcome = await app._pause_and_listen(client, -100, timedelta(seconds=0.1))
            assert outcome == PAUSE_EXPIRED
            assert client.sent == []
            assert all(t is not None for t in client.poll_timeouts)
        finally:
            app.close()


class TestKickDuringPause:
    async def test_removal_from_target_chat_returns_kicked(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient(
                [[make_member_update(GROUP_ID, "kicked", update_id=9)]],
                # discover_chat_id не должен понадобиться до выхода из паузы.
            )
            outcome = await app._pause_and_listen(client, GROUP_ID, timedelta(seconds=60))
            assert outcome == PAUSE_KICKED
        finally:
            app.close()

    async def test_removal_from_other_chat_ignored(self, tmp_path):
        app = make_app(tmp_path)
        try:
            client = FakeClient([[make_member_update(-100777, "kicked", update_id=2)]])
            outcome = await app._pause_and_listen(client, GROUP_ID, timedelta(seconds=0.1))
            assert outcome == PAUSE_EXPIRED
        finally:
            app.close()


class TestRecoverChat:
    async def test_forgets_old_chat_and_saves_new(self, tmp_path):
        app = make_app(tmp_path)
        try:
            app.storage.set_chat_id(GROUP_ID)
            client = FakeClient([], discovered_chat_id=-100999000111)

            new_chat_id = await app._recover_chat(client)

            assert new_chat_id == -100999000111
            assert app.storage.get_chat_id() == -100999000111
        finally:
            app.close()


RSS_ONE = """<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">
    <channel>
        <title>Башорг.рф</title>
        <atom:link href="https://bashorg.local/rss/" rel="self" type="application/rss+xml" />
        <item>
            <guid isPermaLink="false">200000</guid>
            <link>https://bashorg.local/quote/200000</link>
            <title>Цитата #200000</title>
            <pubDate>Sat, 22 Aug 2026 09:10:01 +0300</pubDate>
            <description><![CDATA[Свежая цитата из ленты&lt;br&gt;на случай фолбэка]]></description>
        </item>
    </channel>
</rss>
"""

ARCHIVE_PAGE_199999 = """<article class="quote" data-quote="199999">
            <div class="quote__frame">
                <header class="quote__header">
                    <a class="quote__header_permalink" href="/quote/199999">#199999</a>
                    <div class="quote__header_date">
                        10.05.2021 в 12:00
                    </div>
                </header>
                <div class="quote__body">
                    Старая цитата из архива.
                </div>
                <footer class="quote__footer"></footer>
            </div>
        </article>"""


def _rss_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.startswith("/rss"):
        return httpx.Response(200, text=RSS_ONE)
    if request.url.path == "/quote/199999":
        return httpx.Response(200, text=ARCHIVE_PAGE_199999)
    return httpx.Response(404, text="nope")


class TestPublishCycle:
    async def test_publishes_from_archive(self, tmp_path, monkeypatch):
        """Граница уже известна — берётся случайная цитата из архива."""
        import random

        cfg = Config.load(
            {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "DB_PATH": str(tmp_path / "state.db"),
                "FEED_URL": "https://bashorg.local/rss/",
                "SITE_BASE_URL": "https://bashorg.local",
            }
        )
        app = App(cfg, rng=random.Random(1))
        app.storage.set_archive_start_id(199900)
        try:
            client = FakeClient([])
            rng = random.Random(2)
            monkeypatch.setattr(rng, "randint", lambda a, b: 199999)
            app.rng = rng
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rss_handler)) as http:
                status = await app.publish_cycle(client, http, GROUP_ID)
            assert status == "published"
            assert app.storage.is_published("199999")
            assert client.sent and "Старая цитата из архива." in client.sent[0]
        finally:
            app.close()

    async def test_falls_back_to_feed_when_boundary_unknown(self, tmp_path, monkeypatch):
        """Не удалось найти границу архива — публикуем из свежей ленты."""
        import bot.app as app_module

        async def _no_boundary(http, site_base_url, max_id):
            return None

        monkeypatch.setattr(app_module, "find_archive_start_id", _no_boundary)

        cfg = Config.load(
            {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "DB_PATH": str(tmp_path / "state.db"),
                "FEED_URL": "https://bashorg.local/rss/",
                "SITE_BASE_URL": "https://bashorg.local",
            }
        )
        app = App(cfg)
        try:
            client = FakeClient([])
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rss_handler)) as http:
                status = await app.publish_cycle(client, http, GROUP_ID)
            assert status == "published"
            assert app.storage.is_published("200000")
            assert client.sent and "Свежая цитата из ленты" in client.sent[0]
        finally:
            app.close()

    async def test_nothing_new_when_archive_empty(self, tmp_path, monkeypatch):
        import random

        cfg = Config.load(
            {
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "DB_PATH": str(tmp_path / "state.db"),
                "FEED_URL": "https://bashorg.local/rss/",
                "SITE_BASE_URL": "https://bashorg.local",
            }
        )
        app = App(cfg, rng=random.Random(1))
        app.storage.set_archive_start_id(199900)
        try:
            client = FakeClient([])
            rng = random.Random(2)
            monkeypatch.setattr(rng, "randint", lambda a, b: 199999)
            app.rng = rng
            app.storage.mark_published("199999")
            async with httpx.AsyncClient(transport=httpx.MockTransport(_rss_handler)) as http:
                status = await app.publish_cycle(client, http, GROUP_ID)
            assert status == "nothing_new"
            assert client.sent == []
        finally:
            app.close()
