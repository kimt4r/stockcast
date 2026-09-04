from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


def aggregate_completed_bars(
    bars: list[dict[str, Any]], minutes: int = 3,
) -> list[dict[str, Any]]:
    """Aggregate one-minute bars into completed, clock-aligned bars."""
    if minutes < 1:
        raise ValueError("분봉 집계 주기는 1 이상이어야 합니다.")
    if not bars:
        return []

    parsed: list[tuple[int, dict[str, Any]]] = []
    for bar in bars:
        raw_time = str(bar.get("time") or "")
        if len(raw_time) < 4 or not raw_time[:4].isdigit():
            continue
        hour = int(raw_time[:2])
        minute = int(raw_time[2:4])
        if hour > 23 or minute > 59:
            continue
        parsed.append((hour * 60 + minute, bar))
    if not parsed:
        return []

    current_bucket = parsed[-1][0] // minutes
    grouped: dict[int, list[tuple[int, dict[str, Any]]]] = {}
    for minute_of_day, bar in parsed:
        bucket = minute_of_day // minutes
        if bucket >= current_bucket:
            continue
        grouped.setdefault(bucket, []).append((minute_of_day, bar))

    result: list[dict[str, Any]] = []
    for bucket in sorted(grouped):
        members = grouped[bucket]
        start_minute = bucket * minutes
        if (
            len(members) != minutes
            or {minute for minute, _ in members}
            != set(range(start_minute, start_minute + minutes))
        ):
            continue
        group = [bar for _, bar in members]
        first = group[0]
        last = group[-1]
        result.append({
            "time": f"{start_minute // 60:02d}{start_minute % 60:02d}00",
            "open": int(first.get("open") or first.get("price") or 0),
            "high": max(int(bar.get("high") or bar.get("price") or 0) for bar in group),
            "low": min(int(bar.get("low") or bar.get("price") or 0) for bar in group),
            "price": int(last.get("price") or 0),
            "volume": sum(max(0, int(bar.get("volume") or 0)) for bar in group),
        })
    return result


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


@dataclass(frozen=True)
class GoldenTridentSnapshot:
    signal: Signal
    structure: str
    anchored_vwap: float
    ema: float
    atr: float
    range_atr_ratio: float
    anchor_index: int
    bars_since_flip: int
    anchor_distance_atr: float
    ema_slope_atr: float
    choppy: bool


@dataclass(frozen=True)
class GoldenTridentStrategy:
    """Intraday long-only structure strategy over ordered OHLCV bars."""

    swing_lookback: int = 5
    ema_period: int = 20
    atr_period: int = 14
    chop_lookback: int = 10
    min_range_atr: float = 2.0
    use_ema_filter: bool = True
    use_chop_filter: bool = True

    @staticmethod
    def _value(bar: dict[str, Any], field: str) -> float:
        fallback = bar.get("price") or 0
        return float(bar.get(field) or fallback)

    @staticmethod
    def _ema(values: list[float], period: int) -> float:
        alpha = 2 / (period + 1)
        result = values[0]
        for value in values[1:]:
            result = alpha * value + (1 - alpha) * result
        return result

    @staticmethod
    def _atr(
        highs: list[float], lows: list[float], closes: list[float], period: int,
    ) -> float:
        true_ranges: list[float] = []
        for index in range(1, len(closes)):
            true_ranges.append(max(
                highs[index] - lows[index],
                abs(highs[index] - closes[index - 1]),
                abs(lows[index] - closes[index - 1]),
            ))
        recent = true_ranges[-period:]
        return sum(recent) / len(recent) if recent else 0.0

    def analyze(
        self, bars: list[dict[str, Any]], *, holding: bool = False,
    ) -> GoldenTridentSnapshot:
        if self.swing_lookback < 2 or self.ema_period < 2 or self.atr_period < 2:
            raise ValueError("스윙·EMA·ATR 기간은 2 이상이어야 합니다.")
        if self.chop_lookback < 2 or self.min_range_atr <= 0:
            raise ValueError("횡보 판단 기간과 최소 범위/ATR 배수는 양수여야 합니다.")
        required = max(
            self.swing_lookback + 1,
            self.ema_period if self.use_ema_filter else 2,
            self.atr_period + 1,
            self.chop_lookback,
        )
        if len(bars) < required:
            return GoldenTridentSnapshot(
                signal=Signal.HOLD,
                structure="neutral",
                anchored_vwap=0.0,
                ema=0.0,
                atr=0.0,
                range_atr_ratio=0.0,
                anchor_index=0,
                bars_since_flip=len(bars),
                anchor_distance_atr=0.0,
                ema_slope_atr=0.0,
                choppy=True,
            )

        highs = [self._value(bar, "high") for bar in bars]
        lows = [self._value(bar, "low") for bar in bars]
        closes = [self._value(bar, "price") for bar in bars]
        volumes = [max(0, int(float(bar.get("volume") or 0))) for bar in bars]
        structure = "neutral"
        anchor_index = 0
        for index in range(self.swing_lookback, len(bars)):
            previous_high = max(highs[index - self.swing_lookback:index])
            previous_low = min(lows[index - self.swing_lookback:index])
            breaks_high = highs[index] > previous_high
            breaks_low = lows[index] < previous_low
            next_structure = structure
            if breaks_high and breaks_low:
                midpoint = (highs[index] + lows[index]) / 2
                next_structure = "bullish" if closes[index] >= midpoint else "bearish"
            elif breaks_high:
                next_structure = "bullish"
            elif breaks_low:
                next_structure = "bearish"
            if next_structure != structure and next_structure != "neutral":
                structure = next_structure
                anchor_index = index

        weighted_sum = 0.0
        volume_sum = 0
        for index in range(anchor_index, len(bars)):
            typical_price = (highs[index] + lows[index] + closes[index]) / 3
            weighted_sum += typical_price * volumes[index]
            volume_sum += volumes[index]
        anchored_vwap = weighted_sum / volume_sum if volume_sum else closes[-1]
        ema = self._ema(closes, self.ema_period)
        atr = self._atr(highs, lows, closes, self.atr_period)
        previous_ema = self._ema(closes[:-1], self.ema_period)
        bars_since_flip = (
            len(bars) - 1 - anchor_index if structure != "neutral" else len(bars)
        )
        anchor_distance_atr = (
            (closes[-1] - anchored_vwap) / atr if atr else 0.0
        )
        ema_slope_atr = (ema - previous_ema) / atr if atr else 0.0
        recent_high = max(highs[-self.chop_lookback:])
        recent_low = min(lows[-self.chop_lookback:])
        range_atr_ratio = (recent_high - recent_low) / atr if atr else 0.0
        choppy = self.use_chop_filter and range_atr_ratio < self.min_range_atr

        if holding:
            signal = Signal.SELL if structure == "bearish" else Signal.HOLD
        else:
            entry_allowed = (
                structure == "bullish"
                and closes[-1] > anchored_vwap
                and (not self.use_ema_filter or closes[-1] > ema)
                and not choppy
            )
            signal = Signal.BUY if entry_allowed else Signal.HOLD
        return GoldenTridentSnapshot(
            signal=signal,
            structure=structure,
            anchored_vwap=anchored_vwap,
            ema=ema,
            atr=atr,
            range_atr_ratio=range_atr_ratio,
            anchor_index=anchor_index,
            bars_since_flip=bars_since_flip,
            anchor_distance_atr=anchor_distance_atr,
            ema_slope_atr=ema_slope_atr,
            choppy=choppy,
        )

    def evaluate(self, bars: list[dict[str, Any]], *, holding: bool = False) -> Signal:
        return self.analyze(bars, holding=holding).signal
