"""Тесты генератора случайных интервалов."""

import random
from datetime import timedelta

import pytest

from bot.scheduler import random_interval

HOUR = timedelta(hours=1)


class TestRandomInterval:
    def test_within_bounds_inclusive(self):
        rng = random.Random(42)
        for _ in range(2000):
            interval = random_interval(2.0, 6.0, rng=rng)
            assert timedelta(hours=2) <= interval <= timedelta(hours=6)

    def test_values_differ_between_calls(self):
        rng = random.Random(1)
        values = {random_interval(2.0, 6.0, rng=rng) for _ in range(50)}
        assert len(values) > 10, "интервал должен генерироваться заново каждый раз"

    def test_deterministic_with_seeded_rng(self):
        a = [random_interval(2.0, 6.0, rng=random.Random(7)) for _ in range(5)]
        b = [random_interval(2.0, 6.0, rng=random.Random(7)) for _ in range(5)]
        assert a == b

    def test_returns_whole_seconds(self):
        interval = random_interval(2.0, 6.0, rng=random.Random(3))
        assert interval.microseconds == 0

    @pytest.mark.parametrize("min_hours,max_hours", [(0, 6), (-1, 6), (6, 2), (2, -2)])
    def test_invalid_bounds_raise(self, min_hours, max_hours):
        with pytest.raises(ValueError):
            random_interval(min_hours, max_hours)

    def test_equal_bounds_allowed(self):
        assert random_interval(3.0, 3.0, rng=random.Random(5)) == timedelta(hours=3)
