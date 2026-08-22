"""Генерация случайного интервала между публикациями."""

from __future__ import annotations

import random
from datetime import timedelta


def random_interval(
    min_hours: float,
    max_hours: float,
    rng: random.Random | None = None,
) -> timedelta:
    """Случайная пауза в диапазоне [min_hours; max_hours] часов.

    Каждый вызов генерирует новое значение (никакого фиксированного cron).
    По умолчанию используется SystemRandom — без предсказуемых сидов.
    Для тестов можно передать детерминированный rng.
    """
    if min_hours <= 0 or max_hours < min_hours:
        raise ValueError(f"Требуется 0 < min <= max, получено ({min_hours}, {max_hours})")
    generator = rng if rng is not None else random.SystemRandom()
    seconds = int(generator.uniform(min_hours, max_hours) * 3600)
    return timedelta(seconds=seconds)
