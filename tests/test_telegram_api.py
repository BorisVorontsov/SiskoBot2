"""Тесты автоопределения чата из обновлений Telegram.

Используем SimpleNamespace-заглушки: функция extract_group_chat_id — чистая
логика над атрибутами, а конструирование настоящих объектов PTB сделало бы
тесты хрупкими к версиям библиотеки.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from telegram.error import Conflict

from bot.telegram_api import (
    TelegramClient,
    _extract_private_chat_id,
    extract_group_chat_id,
)


def make_update(
    *,
    my_chat_member=None,
    effective_chat=None,
    update_id=1,
):
    return SimpleNamespace(
        update_id=update_id,
        my_chat_member=my_chat_member,
        effective_chat=effective_chat,
    )


def chat(chat_id: int, chat_type: str) -> SimpleNamespace:
    return SimpleNamespace(id=chat_id, type=chat_type)


def member_update(chat_obj, status: str) -> SimpleNamespace:
    return SimpleNamespace(chat=chat_obj, new_chat_member=SimpleNamespace(status=status))


class TestGroupDetection:
    def test_bot_added_to_group(self):
        update = make_update(
            my_chat_member=member_update(chat(-100999, "supergroup"), "administrator")
        )
        assert extract_group_chat_id(update) == -100999

    def test_bot_added_to_plain_group(self):
        update = make_update(my_chat_member=member_update(chat(-42, "group"), "member"))
        assert extract_group_chat_id(update) == -42

    def test_bot_kicked_is_not_a_target(self):
        update = make_update(my_chat_member=member_update(chat(-100999, "supergroup"), "kicked"))
        assert extract_group_chat_id(update) is None

    def test_private_my_chat_member_ignored(self):
        update = make_update(my_chat_member=member_update(chat(555, "private"), "member"))
        assert extract_group_chat_id(update) is None

    def test_any_group_message_is_a_candidate(self):
        update = make_update(effective_chat=chat(-100777, "supergroup"))
        assert extract_group_chat_id(update) == -100777

    def test_channel_post_ignored(self):
        update = make_update(effective_chat=chat(-100888, "channel"))
        assert extract_group_chat_id(update) is None

    def test_empty_update_is_none(self):
        assert extract_group_chat_id(make_update()) is None


class TestPrivateHint:
    def test_private_message_detected(self):
        update = make_update(effective_chat=chat(555, "private"))
        assert _extract_private_chat_id(update) == 555

    def test_group_message_not_a_hint(self):
        update = make_update(effective_chat=chat(-1, "group"))
        assert _extract_private_chat_id(update) is None


class TestGetUpdatesConflict:
    async def test_conflict_propagates_without_retry(self):
        """Конкурирующий экземпляр с тем же токеном — фейл-фаст, не ретраим."""
        client = TelegramClient("123456:dummy-token")
        calls = []

        class _ConflictingBot:
            async def get_updates(self, **kwargs):
                calls.append(kwargs)
                raise Conflict("terminated by other getUpdates request")

        client._bot = _ConflictingBot()
        with pytest.raises(Conflict):
            await client.poll_updates()
        assert len(calls) == 1


class _ScriptedBot:
    """get_updates возвращает заготовленные батчи и запоминает kwargs."""

    def __init__(self, batches):
        self._batches = [tuple(batches.pop(0)) if batches else () for _ in range(0)]
        self._pending = list(batches)
        self.calls: list[dict] = []

    async def get_updates(self, **kwargs):
        self.calls.append(kwargs)
        if not self._pending:
            return ()
        return tuple(self._pending.pop(0))


def _upd(update_id: int):
    return SimpleNamespace(update_id=update_id, message=None, my_chat_member=None)


class TestUpdatesCursor:
    async def test_offset_advances_and_persists_between_calls(self):
        client = TelegramClient("123456:dummy-token")
        bot = _ScriptedBot([[_upd(4), _upd(7)], [_upd(9)]])
        client._bot = bot

        first = await client.poll_updates(timeout=0)
        assert [u.update_id for u in first] == [4, 7]
        second = await client.poll_updates(timeout=0)

        # Второй вызов обязан уйти с offset=8 — иначе Telegram переиграет батч.
        assert bot.calls[1]["offset"] == 8
        assert [u.update_id for u in second] == [9]
        # Курсор сдвинулся и без внешнего вызова — уйдёт со следующим запросом.
        assert client._updates_offset == 10

    async def test_empty_batch_keeps_cursor(self):
        client = TelegramClient("123456:dummy-token")
        bot = _ScriptedBot([])
        client._bot = bot

        await client.poll_updates(timeout=0)
        await client.poll_updates(timeout=0)
        assert bot.calls[1]["offset"] == 0

    async def test_discover_confirms_batch_before_return(self):
        client = TelegramClient("123456:dummy-token")
        add_event = SimpleNamespace(
            update_id=11,
            message=None,
            my_chat_member=SimpleNamespace(
                chat=SimpleNamespace(id=-100555, type="supergroup"),
                new_chat_member=SimpleNamespace(status="administrator"),
            ),
        )
        bot = _ScriptedBot([[add_event]])
        client._bot = bot

        found = await client.discover_chat_id()
        assert found == -100555
        # Финальный короткий вызов (timeout=0) закрепляет обработанный батч.
        assert bot.calls[-1]["timeout"] == 0
        assert bot.calls[-1]["offset"] == 12

    async def test_flush_drains_queue_until_empty(self):
        client = TelegramClient("123456:dummy-token")
        bot = _ScriptedBot([[_upd(1)], [_upd(2)], []])
        client._bot = bot

        await client.flush_pending_updates()

        assert len(bot.calls) == 3
        assert bot.calls[-1]["offset"] == 3
