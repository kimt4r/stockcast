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


@dataclass(frozen=True)
class TrendBreakoutStrategy:
    """Long-only trend strategy over ordered price bars."""

    short_window: int = 20
    long_window: int = 60
    breakout_window: int = 20
    min_breakout_pct: float = 0.2
    exit_confirmation_bars: int = 2

    def evaluate(self, closes: list[int], *, holding: bool = False) -> Signal:
        if not 1 < self.short_window < self.long_window:
            raise ValueError("이동평균은 1 < 단기 < 장기 순서여야 합니다.")
        if self.breakout_window < 2:
            raise ValueError("돌파 기간은 2개 봉 이상이어야 합니다.")
        if self.min_breakout_pct < 0 or self.exit_confirmation_bars < 1:
            raise ValueError("최소 돌파율은 0 이상, 매도 확인 봉 수는 1 이상이어야 합니다.")
        required = max(self.long_window, self.breakout_window + 1)
        if len(closes) < required:
            return Signal.HOLD

        current = closes[-1]
        short_sma = sum(closes[-self.short_window:]) / self.short_window
        long_sma = sum(closes[-self.long_window:]) / self.long_window
        if holding:
            exit_bars = closes[-self.exit_confirmation_bars:]
            return Signal.SELL if all(price < short_sma for price in exit_bars) else Signal.HOLD

        previous_high = max(closes[-self.breakout_window - 1:-1])
        breakout_pct = (current - previous_high) / previous_high * 100 if previous_high else 0
        if breakout_pct >= self.min_breakout_pct and short_sma > long_sma:
            return Signal.BUY
        return Signal.HOLD
