from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class SmaCrossStrategy:
    short_window: int = 5
    long_window: int = 20

    def evaluate(self, closes: list[int]) -> Signal:
        if self.short_window <= 0 or self.long_window <= self.short_window:
            raise ValueError("이동평균 기간은 0 < short < long이어야 합니다.")
        if len(closes) < self.long_window + 1:
            return Signal.HOLD
        previous_short = sum(closes[-self.short_window - 1:-1]) / self.short_window
        previous_long = sum(closes[-self.long_window - 1:-1]) / self.long_window
        current_short = sum(closes[-self.short_window:]) / self.short_window
        current_long = sum(closes[-self.long_window:]) / self.long_window
        if previous_short <= previous_long and current_short > current_long:
            return Signal.BUY
        if previous_short >= previous_long and current_short < current_long:
            return Signal.SELL
        return Signal.HOLD
