import unittest

from stockcast.strategy import Signal, SmaCrossStrategy, TrendBreakoutStrategy


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


if __name__ == "__main__":
    unittest.main()
