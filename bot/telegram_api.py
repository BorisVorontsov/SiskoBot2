"""Работа с официальным Telegram Bot API через библиотеку python-telegram-bot.

Две задачи:
* публикация сообщений в группу с обработкой rate limit, сетевых сбоев и бана;
* автоопределение чата, если TELEGRAM_CHAT_ID не задан: бот слушает обновления
  и берёт ID группы, в которую его добавили (или любого сообщения в группе).
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Bot, Chat, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

log = logging.getLogger(__name__)

_NETWORK_BACKOFF_START = 2.0
_NETWORK_BACKOFF_CAP = 60.0
_MAX_NETWORK_ATTEMPTS = 8

GROUP_CHAT_TYPES = frozenset({Chat.GROUP, Chat.SUPERGROUP})
# Статусы бота, при которых он присутствует в чате и может писать.
_PRESENT_STATUSES = frozenset({"member", "administrator", "restricted"})


class BotKickedError(Exception):
    """Бот удалён из чата — публиковать некуда до повторного добавления."""


class TelegramClient:
    """Обёртка над python-telegram-bot для сценария «только публикуем»."""

    def __init__(self, token: str, poll_timeout: int = 25):
        self._poll_timeout = poll_timeout
        # Курсор обновлений getUpdates. Telegram переигрывает все неподтверждённые
        # события при каждом вызове, поэтому курсор обязан жить в клиенте и
        # переживать повторные входы в автоопределение/прослушивание — иначе
        # старые «добавили/удалили» срабатывают бесконечно.
        self._updates_offset = 0
        # PTB требует отдельный экземпляр request под getUpdates из-за long polling.
        self._bot = Bot(
            token=token,
            request=_make_request(),
            get_updates_request=_make_request(read_timeout=poll_timeout + 15),
        )

    async def __aenter__(self) -> TelegramClient:
        await self._bot.initialize()
        return self

    async def __aexit__(self, *exc_info) -> None:
        await self._bot.shutdown()

    async def send_messages(self, chat_id: int, texts: list[str]) -> None:
        """Отправляет фрагменты по очереди; при сбое кидает исключение.

        Цитата помечается опубликованной только после успешного возврата,
        поэтому частичная отправка длинной цитаты приведёт к повтору цикла —
        это осознанный компромисс ради «не терять посты молча».
        """
        for text in texts:
            message = await self._send_with_retry(chat_id, text)
            log.info("Сообщение доставлено (message_id=%s)", message.message_id)

    async def _send_with_retry(self, chat_id: int, text: str):
        delay = _NETWORK_BACKOFF_START
        network_failures = 0
        while True:
            try:
                return await self._bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
            except RetryAfter as exc:
                wait = float(exc.retry_after) + 1.0
                log.warning("Telegram rate limit: ждём %.0f сек", wait)
                await asyncio.sleep(wait)
            except (NetworkError, TimedOut) as exc:
                network_failures += 1
                if network_failures >= _MAX_NETWORK_ATTEMPTS:
                    raise
                log.warning(
                    "Сетевая ошибка Telegram (%s: %s), попытка %d/%d, пауза %.0f сек",
                    type(exc).__name__,
                    exc,
                    network_failures,
                    _MAX_NETWORK_ATTEMPTS,
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _NETWORK_BACKOFF_CAP)
            except Forbidden as exc:
                raise BotKickedError(f"Бот исключён или заблокирован в чате {chat_id}") from exc
            except BadRequest:
                # Постоянная ошибка (чат не найден, неверная разметка и т.п.) —
                # ретраить бессмысленно.
                raise

    async def discover_chat_id(self) -> int:
        """Ждёт добавления бота в группу и возвращает ID этого чата.

        Блокируется на long polling; завершается, как только найдена группа.
        Курсор обновлений ведётся клиентом, поэтому уже сыгранные события
        не приходят повторно при следующем входе в автоопределение.
        Личные сообщения пользователей логируются как подсказка про CHAT_ID.
        """
        log.info(
            "CHAT_ID не задан: жду добавления в группу (long polling, таймаут %d сек)...",
            self.poll_timeout,
        )
        while True:
            updates = await self.poll_updates()
            for update in updates:
                found = extract_group_chat_id(update)
                if found is not None:
                    log.info("Найден целевой чат: %s", found)
                    # Подтверждаем обработанный батч немедленно, чтобы событие
                    # «добавили в группу» не переиграло после перезапуска цикла.
                    await self._confirm_updates()
                    return found
                hint = _extract_private_chat_id(update)
                if hint is not None:
                    log.info(
                        "Личное сообщение от %s. Для приватных чатов можно задать "
                        "TELEGRAM_CHAT_ID=%s",
                        hint,
                        hint,
                    )

    async def _confirm_updates(self) -> None:
        """Одна короткая сверка, закрепляющая текущий курсор обновлений."""
        try:
            await self._bot.get_updates(
                offset=self._updates_offset,
                timeout=0,
                allowed_updates=["message", "my_chat_member"],
            )
        except Exception:  # подтверждение некритично: курсор уйдёт со следующим вызовом
            log.debug("Подтверждение обновлений не удалось — продолжаем", exc_info=True)

    async def flush_pending_updates(self, max_batches: int = 40) -> None:
        """Выбрасывает накопившиеся старые обновления.

        Нужен на старте, когда целевой чат уже известен: устаревшие события
        членства (давние добавления/удаления) не должны влиять на работу,
        но и копиться неподтверждёнными тоже нельзя.
        """
        for _ in range(max_batches):
            updates = await self.poll_updates(timeout=0)
            if not updates:
                return
        log.warning("Очередь обновлений не опустела за %d батчей — продолжаю", max_batches)

    @property
    def poll_timeout(self) -> int:
        """Long-polling таймаут getUpdates в секундах."""
        return self._poll_timeout

    async def poll_updates(self, timeout: float | None = None) -> tuple[Update, ...]:
        """Батч обновлений с ретраями сети; Conflict пробрасывается наверх.

        offset берётся из внутреннего курса клиента и автоматически
        сдвигается по полученному батчу. Малый timeout нужен основному
        циклу, чтобы оставаться отзывчивым к дедлайну публикации.
        """
        delay = _NETWORK_BACKOFF_START
        while True:
            try:
                updates = await self._bot.get_updates(
                    offset=self._updates_offset,
                    timeout=self._poll_timeout if timeout is None else max(int(timeout), 0),
                    allowed_updates=["message", "my_chat_member"],
                )
            except RetryAfter as exc:
                wait = float(exc.retry_after) + 1.0
                log.warning("getUpdates rate limit: ждём %.0f сек", wait)
                await asyncio.sleep(wait)
            except Conflict:
                # Второй экземпляр с тем же токеном перехватил getUpdates —
                # ретраи бессмысленны, наверх уходит внятная ошибка (exit 4).
                raise
            except (NetworkError, TimedOut):
                # Long polling иногда рвётся сам по себе — это норма, просто ждём.
                await asyncio.sleep(delay)
                delay = min(delay * 2, _NETWORK_BACKOFF_CAP)
            else:
                if updates:
                    self._updates_offset = max(u.update_id for u in updates) + 1
                return updates


def extract_group_chat_id(update: Update) -> int | None:
    """ID группы, куда добавили бота или где появилось сообщение."""
    member_update = update.my_chat_member
    if member_update is not None:
        chat = member_update.chat
        status = member_update.new_chat_member.status
        if chat.type in GROUP_CHAT_TYPES and status in _PRESENT_STATUSES:
            return int(chat.id)

    chat = update.effective_chat
    if chat is not None and chat.type in GROUP_CHAT_TYPES:
        return int(chat.id)
    return None


def _extract_private_chat_id(update: Update) -> int | None:
    chat = update.effective_chat
    if chat is not None and chat.type == Chat.PRIVATE:
        return int(chat.id)
    return None


_REMOVED_STATUSES = frozenset({"left", "kicked"})


def bot_removed_from(update: Update, target_chat_id: int) -> bool:
    """True, если бота удалили (или кикнули) именно из целевого чата."""
    member_update = update.my_chat_member
    if member_update is None:
        return False
    chat = member_update.chat
    status = member_update.new_chat_member.status
    return int(chat.id) == int(target_chat_id) and status in _REMOVED_STATUSES


def _make_request(read_timeout: float = 30.0) -> HTTPXRequest:
    return HTTPXRequest(connect_timeout=15, read_timeout=read_timeout, write_timeout=30)


__all__ = [
    "BotKickedError",
    "TelegramClient",
    "bot_removed_from",
    "extract_group_chat_id",
]
