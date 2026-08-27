"""SQLite-хранилище опубликованных цитат и служебного состояния.

Состояние переживает перезапуск процесса и пересоздание контейнера
(каталог с базой монтируется как volume). SQLite достаточно для такого
объёма данных: одна строка на цитату и пара ключей состояния.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS published (
    quote_id     TEXT PRIMARY KEY,
    published_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_CHAT_ID_KEY = "chat_id"
_ARCHIVE_START_KEY = "archive_start_id"


class Storage:
    """Тонкая обёртка над SQLite. Один процесс — одно соединение."""

    def __init__(self, path: str | Path):
        self._path = Path(path)
        parent = self._path.parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self._path, isolation_level=None)
        # WAL переживает жёсткую остановку контейнера лучше журнала по умолчанию.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        log.info("Хранилище открыто: %s", self._path)

    def close(self) -> None:
        self._conn.close()

    # --- опубликованные цитаты -------------------------------------------------

    def mark_published(self, quote_id: str) -> None:
        """Отмечает цитату опубликованной. Повторный вызов ничего не меняет."""
        self._conn.execute(
            "INSERT OR IGNORE INTO published (quote_id, published_at) VALUES (?, ?)",
            (quote_id, _utcnow_iso()),
        )

    def is_published(self, quote_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM published WHERE quote_id = ?", (quote_id,)
        ).fetchone()
        return row is not None

    def published_ids(self) -> set[str]:
        rows = self._conn.execute("SELECT quote_id FROM published").fetchall()
        return {row[0] for row in rows}

    def count_published(self) -> int:
        return int(self._conn.execute("SELECT COUNT(*) FROM published").fetchone()[0])

    # --- служебное состояние ---------------------------------------------------

    def get_chat_id(self) -> int | None:
        row = self._conn.execute(
            "SELECT value FROM state WHERE key = ?", (_CHAT_ID_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except ValueError:
            log.warning("В хранилище повреждён chat_id=%r — игнорирую", row[0])
            return None

    def set_chat_id(self, chat_id: int) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_CHAT_ID_KEY, str(chat_id)),
        )

    def delete_chat_id(self) -> None:
        """Забывает сохранённый чат (после кика — для автоопределения нового)."""
        self._conn.execute("DELETE FROM state WHERE key = ?", (_CHAT_ID_KEY,))

    def get_archive_start_id(self) -> int | None:
        """Первая цитата пула (ID с датой ARCHIVE_START_DATE), либо None."""
        row = self._conn.execute(
            "SELECT value FROM state WHERE key = ?", (_ARCHIVE_START_KEY,)
        ).fetchone()
        if row is None:
            return None
        try:
            return int(row[0])
        except ValueError:
            log.warning("В хранилище повреждён archive_start_id=%r — игнорирую", row[0])
            return None

    def set_archive_start_id(self, start_id: int) -> None:
        self._conn.execute(
            "INSERT INTO state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_ARCHIVE_START_KEY, str(start_id)),
        )


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
