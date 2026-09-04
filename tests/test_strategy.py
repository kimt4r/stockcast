import unittest

from stockcast.strategy import (
    GoldenTridentStrategy,
    Signal,
    SmaCrossStrategy,
    TrendBreakoutStrategy,
    aggregate_completed_bars,
)


def ohlcv_bars(prices, *, spread=2, volumes=None):
    volumes = volumes or [100] * len(prices)
    return [
        {
            "price": price,
            "open": price,
            "high": price + spread / 2,
            "low": price - spread / 2,
            "volume": volumes[index],
        }
        for index, price in enumerate(prices)
    ]


class BarAggregationTests(unittest.TestCase):
    def test_aggregates_only_completed_clock_aligned_three_minute_bars(self):
        bars = [
            {
                "time": f"09{minute:02d}00", "open": 100 + minute,
                "high": 102 + minute, "low": 99 + minute,
                "price": 101 + minute, "volume": 10 + minute,
            }
            for minute in range(7)
        ]

        result = aggregate_completed_bars(bars, 3)

        self.assertEqual([bar["time"] for bar in result], ["090000", "090300"])
        self.assertEqual(result[0], {
            "time": "090000", "open": 100, "high": 104, "low": 99,
            "price": 103, "volume": 33,
        })

    def test_rejects_invalid_aggregation_period(self):
        with self.assertRaisesRegex(ValueError, "집계 주기"):
            aggregate_completed_bars([], 0)


class SmaCrossStrategyTests(unittest.TestCase):
    def test_buy_on_upward_cross(self):
        strategy = SmaCrossStrategy(short_window=2, long_window=3)
        self.assertEqual(strategy.evaluate([5, 5, 5, 4, 7]), Signal.BUY)

    def test_sell_on_downward_cross(self):
        strategy = SmaCrossStrategy(short_window=2, long_window=3)
        self.assertEqual(strategy.evaluate([5, 5, 5, 6, 3]), Signal.SELL)

    def test_hold_when_history_is_short(self):
        self.assertEqual(SmaCrossStrategy().evaluate([1, 2, 3]), Signal.HOLD)


class TrendBreakoutStrategyTests(unittest.TestCase):
    def test_buys_only_when_twenty_day_high_breaks_in_uptrend(self):
        closes = list(range(100, 160)) + [200]

        self.assertEqual(TrendBreakoutStrategy().evaluate(closes), Signal.BUY)

    def test_holds_without_breakout(self):
        closes = list(range(100, 161))
        closes[-1] = closes[-2]

        self.assertEqual(TrendBreakoutStrategy().evaluate(closes), Signal.HOLD)

    def test_sells_holding_below_twenty_day_average(self):
        closes = list(range(100, 159)) + [100, 99]

        self.assertEqual(
            TrendBreakoutStrategy().evaluate(closes, holding=True), Signal.SELL
        )

    def test_holds_after_only_one_bar_below_average(self):
        closes = list(range(100, 160)) + [100]

        self.assertEqual(
            TrendBreakoutStrategy().evaluate(closes, holding=True), Signal.HOLD
        )

    def test_requires_minimum_breakout_buffer(self):
        closes = [10_000] * 20 + [10_010]

        self.assertEqual(
            TrendBreakoutStrategy(
                short_window=5, long_window=20, breakout_window=5,
                min_breakout_pct=0.2,
            ).evaluate(closes),
            Signal.HOLD,
        )


class GoldenTridentStrategyTests(unittest.TestCase):
    def test_buys_bullish_structure_above_anchor_and_ema(self):
        snapshot = GoldenTridentStrategy().analyze(ohlcv_bars(list(range(100, 130))))

        self.assertEqual(snapshot.signal, Signal.BUY)
        self.assertEqual(snapshot.structure, "bullish")
        self.assertGreater(129, snapshot.anchored_vwap)
        self.assertGreater(129, snapshot.ema)
        self.assertFalse(snapshot.choppy)

    def test_sells_only_after_bearish_structure_flip(self):
        prices = list(range(100, 120)) + [118, 117, 116, 115, 110]

        snapshot = GoldenTridentStrategy().analyze(ohlcv_bars(prices), holding=True)

        self.assertEqual(snapshot.signal, Signal.SELL)
        self.assertEqual(snapshot.structure, "bearish")
        self.assertGreater(snapshot.anchor_index, 0)

    def test_chop_filter_blocks_flat_market(self):
        strategy = GoldenTridentStrategy(min_range_atr=2)

        snapshot = strategy.analyze(ohlcv_bars([100] * 25, spread=0.2))

        self.assertEqual(snapshot.signal, Signal.HOLD)
        self.assertTrue(snapshot.choppy)

    def test_anchored_vwap_resets_on_latest_structure_flip(self):
        prices = list(range(100, 115)) + list(range(114, 104, -1)) + list(range(106, 116))

        snapshot = GoldenTridentStrategy(use_ema_filter=False).analyze(ohlcv_bars(prices))

        self.assertGreater(snapshot.anchor_index, 20)
        self.assertGreater(snapshot.anchored_vwap, 105)

    def test_reports_structure_freshness_atr_distance_and_ema_slope(self):
        prices = [30] * 15 + [29, 28, 27, 26, 25, 24, 23, 22, 23, 24, 25, 26, 27, 28, 29]

        snapshot = GoldenTridentStrategy().analyze(ohlcv_bars(prices, spread=2))

        self.assertEqual(snapshot.bars_since_flip, 4)
        self.assertAlmostEqual(snapshot.anchor_distance_atr, 1.0)
        self.assertGreater(snapshot.ema_slope_atr, 0)


if __name__ == "__main__":
    unittest.main()
