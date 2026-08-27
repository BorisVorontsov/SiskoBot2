"""Тесты хранилища: персистентность, идемпотентность, состояние чата."""

from __future__ import annotations

from bot.storage import Storage


class TestPublishedQuotes:
    def test_mark_and_check(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        assert not storage.is_published("470009")
        storage.mark_published("470009")
        assert storage.is_published("470009")
        assert storage.published_ids() == {"470009"}
        assert storage.count_published() == 1
        storage.close()

    def test_duplicate_mark_is_idempotent(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        storage.mark_published("1")
        storage.mark_published("1")
        assert storage.count_published() == 1
        storage.close()

    def test_state_survives_restart(self, tmp_path):
        path = tmp_path / "state.db"
        first = Storage(path)
        first.mark_published("100")
        first.mark_published("200")
        first.set_chat_id(-1001234567890)
        first.close()

        second = Storage(path)
        assert second.published_ids() == {"100", "200"}
        assert second.count_published() == 2
        assert second.get_chat_id() == -1001234567890
        second.close()


class TestChatState:
    def test_default_is_none(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        assert storage.get_chat_id() is None
        storage.close()

    def test_set_overwrites(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        storage.set_chat_id(111)
        storage.set_chat_id(222)
        assert storage.get_chat_id() == 222
        storage.close()

    def test_delete_forgets_chat(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        storage.set_chat_id(-1001541901987)
        storage.delete_chat_id()
        assert storage.get_chat_id() is None
        # Повторное удаление безопасно.
        storage.delete_chat_id()
        assert storage.get_chat_id() is None
        storage.close()

    def test_corrupted_value_ignored(self, tmp_path):
        path = tmp_path / "state.db"
        storage = Storage(path)
        storage._conn.execute("INSERT INTO state (key, value) VALUES ('chat_id', 'не число')")
        assert storage.get_chat_id() is None
        storage.close()


class TestArchiveState:
    def test_default_is_none(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        assert storage.get_archive_start_id() is None
        storage.close()

    def test_round_trip(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        storage.set_archive_start_id(155000)
        assert storage.get_archive_start_id() == 155000
        storage.close()

    def test_set_overwrites(self, tmp_path):
        storage = Storage(tmp_path / "state.db")
        storage.set_archive_start_id(1)
        storage.set_archive_start_id(255000)
        assert storage.get_archive_start_id() == 255000
        storage.close()

    def test_corrupted_value_ignored(self, tmp_path):
        path = tmp_path / "state.db"
        storage = Storage(path)
        storage._conn.execute("INSERT INTO state (key, value) VALUES ('archive_start_id', 'много')")
        assert storage.get_archive_start_id() is None
        storage.close()

    def test_survives_restart(self, tmp_path):
        path = tmp_path / "state.db"
        first = Storage(path)
        first.set_archive_start_id(300000)
        first.close()

        second = Storage(path)
        assert second.get_archive_start_id() == 300000
        second.close()


class TestFilteringScenario:
    def test_fresh_quotes_filtered_against_storage(self, tmp_path):
        """Сценарий выбора: из ленты отсеиваются уже опубликованные."""
        storage = Storage(tmp_path / "state.db")
        for quote_id in ("470007", "470008"):
            storage.mark_published(quote_id)

        feed_ids = ["470009", "470008", "470007"]
        seen = storage.published_ids()
        fresh = [qid for qid in feed_ids if qid not in seen]
        assert fresh == ["470009"]
        storage.close()
