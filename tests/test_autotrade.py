import unittest
from unittest.mock import Mock, patch
import json
from datetime import datetime
from pathlib import Path
import tempfile

from stockcast.autotrade import KST, AutoTradeConfig, AutoTrader
from stockcast.config import Settings


def intraday_bars(prices):
    volumes = [100] * len(prices)
    if len(volumes) >= 2:
        volumes[-2] = 200
    return [
        {
            "time": f"09{index:02d}00", "open": price, "high": price + 1,
            "low": price - 1, "price": price, "volume": volumes[index],
        }
        for index, price in enumerate(prices)
    ]


def fresh_bullish_prices():
    return [30] * 15 + [29, 28, 27, 26, 25, 24, 23, 22, 23, 24, 25, 26, 27, 28, 29]


def three_minute_prices(completed, current=None):
    prices = [price for price in completed for _ in range(3)]
    prices.extend([completed[-1] if current is None else current] * 3)
    return intraday_bars(prices)


class AutoTraderTests(unittest.TestCase):
    def setUp(self):
        self._report_temp = tempfile.TemporaryDirectory()
        self.report_dir = Path(self._report_temp.name)

    def tearDown(self):
        self._report_temp.cleanup()

    def test_accepts_alphanumeric_inverse_etf_symbol(self):
        AutoTradeConfig(("0193L0",), auto_discover=False).validate()

    def test_buys_uptrend_in_paper_account(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {"output1": [], "output2": [{"tot_evlu_amt": "1000"}]}
        client.intraday_bars.return_value = three_minute_prices(
            [30, 29, 28, 27, 26, 25, 26, 27, 28]
        )
        client.order.return_value = {"output": {"ODNO": "12345"}}
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            ("005930",), auto_discover=False, position_size_pct=6,
        )

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.symbol, order.side, order.quantity), ("005930", "buy", 2))
        self.assertEqual(trader._daily_orders[-1]["order_id"], "12345")
        self.assertEqual(trader._daily_orders[-1]["reason"], "golden_trident_entry")
        self.assertIn("catastrophe_stop", trader._daily_orders[-1])
        self.assertEqual(trader._daily_orders[-1]["signal_bar_minutes"], 3)
        self.assertEqual(trader._daily_orders[-1]["risk_bar_minutes"], 1)

    def test_does_not_buy_from_spike_in_unfinished_three_minute_bar(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = three_minute_prices(
            [30, 29, 28, 27, 26, 25, 24, 23, 22], current=100,
        )
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        client.order.assert_not_called()

    def test_sells_at_catastrophe_stop(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "200"}],
            "output2": [],
        }
        client.intraday_bars.return_value = intraday_bars([200] * 20 + [100])
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False, position_size_pct=10)
        trader._managed_positions.add("005930")
        trader._trading_date = datetime.now(KST).date().isoformat()
        trader._catastrophe_stops["005930"] = 150

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 3))
        self.assertEqual(trader._daily_orders[-1]["reason"], "catastrophe_stop")

    def test_live_account_cannot_start(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="live")
        trader = AutoTrader(client, self.report_dir)

        with self.assertRaisesRegex(ValueError, "모의투자"):
            trader.start(AutoTradeConfig(("005930",)))

    def test_discovers_and_selects_uptrend_from_volume_rank(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.daytrade_rank.return_value = [
            {"mksc_shrn_iscd": "111111"},
            {"mksc_shrn_iscd": "222222"},
        ]
        client.intraday_bars.side_effect = lambda symbol: three_minute_prices(
            [30, 29, 28, 27, 26, 25, 26, 27, 28]
            if symbol == "111111" else [30, 29, 28, 27, 26, 25, 24, 23, 22]
        )
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(auto_discover=True, scan_limit=2, select_count=1)

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual(order.symbol, "111111")
        self.assertTrue(any("111111" in event["message"] for event in trader.status()["events"]))
        report = json.loads(Path(
            trader.status()["daily_performance"]["report_path"]
        ).read_text(encoding="utf-8"))
        detail = report["telemetry"]["last_selection_details"][0]
        self.assertEqual(detail["symbol"], "111111")
        self.assertLessEqual(detail["bars_since_flip"], 5)
        self.assertLessEqual(detail["anchor_distance_atr"], 1.5)
        self.assertGreater(detail["ema_slope_atr"], 0)

    def test_discovery_rejects_stale_overextended_trend(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.daytrade_rank.return_value = [{"mksc_shrn_iscd": "111111"}]
        client.intraday_bars.return_value = intraday_bars(list(range(1, 31)))
        trader = AutoTrader(client, self.report_dir)
        config = AutoTradeConfig(auto_discover=True, scan_limit=1, select_count=1)

        symbols, _ = trader._discover(config)

        self.assertEqual(symbols, [])
        self.assertEqual(trader._scan_rejections["structure_stale"], 1)
        self.assertEqual(trader._scan_rejections["overextended_from_anchored_vwap"], 1)

    def test_auto_discovery_is_skipped_outside_entry_window(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(auto_discover=True)

        with patch.object(trader, "_entry_is_open", return_value=False):
            trader.run_once()

        client.daytrade_rank.assert_not_called()
        self.assertTrue(any(
            "자동 종목 검색을 생략" in event["message"]
            for event in trader.status()["events"]
        ))

    def test_validates_freshness_and_anchor_distance_ranges(self):
        with self.assertRaisesRegex(ValueError, "추세 판단 분봉"):
            AutoTradeConfig(signal_bar_minutes=0).validate()
        with self.assertRaisesRegex(ValueError, "신선도"):
            AutoTradeConfig(max_structure_age_bars=0).validate()
        with self.assertRaisesRegex(ValueError, "앵커 VWAP 이격"):
            AutoTradeConfig(
                min_anchor_distance_atr=2, max_anchor_distance_atr=1,
            ).validate()

    def test_force_exit_sells_all_bot_managed_quantity(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "4", "pchs_avg_pric": "100"}],
            "output2": [],
        }
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)
        trader._managed_positions.add("005930")

        trader.run_once(force_exit=True)

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 4))
        self.assertNotIn("005930", trader._managed_positions)

    def test_positive_daily_return_does_not_block_new_buy(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1100000"}],
        }
        client.intraday_bars.return_value = three_minute_prices(
            [30, 29, 28, 27, 26, 25, 26, 27, 28]
        )
        with tempfile.TemporaryDirectory() as directory:
            trader = AutoTrader(client, Path(directory))
            trader._config = AutoTradeConfig(("005930",), auto_discover=False, position_size_pct=10)
            trader._trading_date = datetime.now(KST).date().isoformat()
            trader._starting_equity = 1_000_000

            with patch.object(trader, "_entry_is_open", return_value=True):
                trader.run_once()

            client.order.assert_called_once()
            self.assertNotIn("target_reached", trader.status()["daily_performance"])

    def test_position_size_uses_equity_and_only_reduces_risk(self):
        client = Mock()
        client.settings = Settings(
            "key", "secret", "12345678", environment="paper", max_order_krw=1_000_000,
        )
        trader = AutoTrader(client, self.report_dir)
        trader._latest_equity = 1_000_000
        config = AutoTradeConfig(position_size_pct=10)

        self.assertEqual(trader._order_quantity(config, 10_000, None), 10)
        self.assertEqual(trader._order_quantity(config, 10_000, 2), 10)
        self.assertEqual(trader._order_quantity(config, 10_000, -0.1), 5)
        self.assertEqual(trader._order_quantity(config, 10_000, 5), 10)

    def test_writes_daily_report_from_account_equity(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 30)

        with tempfile.TemporaryDirectory() as directory:
            trader = AutoTrader(client, Path(directory))
            trader._config = AutoTradeConfig(("005930",), auto_discover=False)
            trader.run_once()
            report_path = Path(trader.status()["daily_performance"]["report_path"])
            report = json.loads(report_path.read_text(encoding="utf-8"))
            journal_path = Path(trader.status()["daily_performance"]["journal_path"])
            journal_text = journal_path.read_text(encoding="utf-8")

        self.assertEqual(report["starting_equity"], 1_000_000)
        self.assertEqual(report["return_pct"], 0.0)
        self.assertNotIn("target_pct", report)
        self.assertIn("총평가금액", report["measurement"])
        self.assertIn("journal", report)
        self.assertEqual(report["telemetry"]["run_count"], 1)
        self.assertIn("## 문제점", journal_text)
        self.assertIn("주문이 한 건도 없었습니다", journal_text)

    def test_disconnect_creates_incrementing_daily_report_versions(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 30)
        client.daily_executions.return_value = []
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)
        trader.run_once()

        first = trader.disconnect()
        second = trader.disconnect()

        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        first_json = json.loads(Path(first["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(first_json["report_version"], 1)
        self.assertEqual(first_json["status"], "stopped")
        self.assertIn("보존 버전: v1", Path(first["journal_path"]).read_text(encoding="utf-8"))
        self.assertTrue(Path(second["json_path"]).exists())
        self.assertTrue(Path(second["journal_path"]).exists())

    def test_existing_daily_report_is_preserved_as_version_one(self):
        today = datetime.now(KST).date().isoformat()
        base_report = {"date": today, "status": "running", "marker": "기존 진행분"}
        (self.report_dir / f"{today}.json").write_text(
            json.dumps(base_report, ensure_ascii=False), encoding="utf-8",
        )
        (self.report_dir / f"{today}.md").write_text(
            f"# {today} 데이트레이딩 리포트\n\n기존 진행분\n", encoding="utf-8",
        )
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)

        archived = trader._ensure_initial_report_version()

        self.assertEqual(archived["version"], 1)
        version_one = json.loads(Path(archived["json_path"]).read_text(encoding="utf-8"))
        self.assertEqual(version_one["marker"], "기존 진행분")
        self.assertEqual(version_one["report_version"], 1)

    def test_records_user_confirmed_manual_liquidation_without_inventing_fill(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{
                "pdno": "114800", "hldg_qty": "472", "pchs_avg_pric": "1045",
                "prpr": "1079", "prdt_name": "KODEX 인버스",
            }],
            "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([1079] * 30)
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("114800",), auto_discover=False)
        trader._managed_positions.add("114800")
        trader.run_once()

        intervention = trader.record_manual_liquidation()
        report = json.loads((
            self.report_dir / f"{datetime.now(KST).date().isoformat()}.json"
        ).read_text(encoding="utf-8"))
        markdown = (
            self.report_dir / f"{datetime.now(KST).date().isoformat()}.md"
        ).read_text(encoding="utf-8")

        self.assertEqual(intervention["positions"][0]["last_known_quantity"], 472)
        self.assertIsNone(intervention["positions"][0]["reported_sell_price"])
        self.assertEqual(report["status"], "manual_liquidation_reported")
        self.assertEqual(report["end_positions"], [])
        self.assertEqual(report["manual_interventions"][0]["source"], "user_confirmation")
        self.assertIn("사용자 확인 전량청산", markdown)
        self.assertIn("체결가와 실제 매도수량은 미확인", markdown)

        restored = AutoTrader(client, self.report_dir)
        restored._restore_daily_report()
        self.assertNotIn("114800", restored._managed_positions)
        self.assertEqual(len(restored._manual_interventions), 1)

    def test_execution_analysis_matches_strategy_order_number(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        trader._daily_orders = [
            {"order_id": "100", "symbol": "005930", "side": "buy", "quantity": 2},
            {"order_id": "101", "symbol": "005930", "side": "sell", "quantity": 2},
        ]
        trader._executions = [
            {
                "ord_dt": "20260902", "ord_tmd": "100000", "odno": "100",
                "pdno": "005930", "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd_name": "매수", "tot_ccld_qty": "2",
                "avg_prvs": "70000", "rmn_qty": "0",
            },
            {
                "ord_dt": "20260902", "ord_tmd": "110000", "odno": "101",
                "pdno": "005930", "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd_name": "매도", "tot_ccld_qty": "2",
                "avg_prvs": "71000", "rmn_qty": "0",
            },
            {
                "ord_dt": "20260902", "ord_tmd": "120000", "odno": "manual",
                "pdno": "000660", "sll_buy_dvsn_cd_name": "매수",
                "tot_ccld_qty": "1", "avg_prvs": "100000", "rmn_qty": "0",
            },
        ]

        analysis = trader._execution_analysis()

        self.assertEqual(analysis["account_execution_rows"], 3)
        self.assertEqual(analysis["strategy_execution_rows"], 2)
        self.assertEqual(analysis["unconfirmed_order_count"], 0)
        self.assertEqual(analysis["fill_rate_pct"], 100.0)
        self.assertEqual(analysis["gross_realized_pnl_krw"], 2_000)
        self.assertEqual(analysis["wins"], 1)

    def test_no_trade_report_records_precise_decision_blocks(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 30)
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)

        with patch.object(trader, "_entry_is_open", return_value=False):
            trader.run_once()

        report_path = Path(trader.status()["daily_performance"]["report_path"])
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertGreater(report["telemetry"]["decision_counts"]["outside_entry_window"], 0)
        self.assertGreater(report["telemetry"]["decision_counts"]["structure_not_bullish"], 0)
        self.assertGreater(report["telemetry"]["decision_counts"]["choppy_market"], 0)

    def test_reentry_is_blocked_during_cooldown_and_after_daily_limit(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        config = AutoTradeConfig(reentry_cooldown_minutes=30, max_entries_per_symbol=2)
        trader._last_exits["005930"] = datetime.now(KST)

        self.assertFalse(trader._can_reenter("005930", config))
        trader._last_exits.clear()
        trader._entry_counts["005930"] = 2
        self.assertFalse(trader._can_reenter("005930", config))

    def test_round_trip_limit_reserves_slots_for_open_positions(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [], "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = three_minute_prices(
            [30, 29, 28, 27, 26, 25, 26, 27, 28]
        )
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            ("005930",), auto_discover=False, max_daily_round_trips=10,
        )
        trader._trading_date = datetime.now(KST).date().isoformat()
        trader._starting_equity = 1_000_000
        trader._closed_trades = 8
        trader._managed_positions.update({"111111", "222222"})

        with patch.object(trader, "_entry_is_open", return_value=True):
            trader.run_once()

        client.order.assert_not_called()
        self.assertEqual(trader._decision_counts["max_round_trips"], 1)
        performance = trader.status()["daily_performance"]
        self.assertEqual(performance["round_trip_slots_used"], 10)
        self.assertEqual(performance["round_trip_slots_remaining"], 0)

    def test_review_flags_trade_limit_concentration_and_reentry_loss(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(max_daily_round_trips=10)
        trader._daily_orders = [{"side": "buy"}]
        trader._starting_equity = 1_000_000
        trader._latest_equity = 1_015_100
        trader._peak_return_pct = 2.0
        trader._decision_counts["max_round_trips"] = 5
        execution = {
            "unfilled_quantity": 0,
            "unconfirmed_order_count": 0,
            "closed_trade_count": 12,
            "win_rate_pct": 33.33,
            "gross_realized_pnl_krw": 400,
            "trades": [
                {"symbol": "111111", "gross_pnl_krw": 1_000},
                {"symbol": "222222", "gross_pnl_krw": -100},
                {"symbol": "222222", "gross_pnl_krw": -200},
                {"symbol": "333333", "gross_pnl_krw": -100},
                {"symbol": "444444", "gross_pnl_krw": -100},
                {"symbol": "555555", "gross_pnl_krw": -100},
            ],
        }

        review = trader._review(execution)
        issues = " ".join(review["issues"])

        self.assertIn("설정 상한 10회를 넘어 12회", issues)
        self.assertIn("승률이 33.33%", issues)
        self.assertIn("총 이익의 100.0%", issues)
        self.assertIn("재진입 1건", issues)
        self.assertIn("0.49%p 반납", issues)

    def test_profit_lock_activates_after_peak_giveback(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            profit_lock_activation_pct=1, profit_giveback_pct=0.5,
        )
        trader._starting_equity = 1_000_000
        trader._latest_equity = 1_006_000
        trader._peak_return_pct = 1.2

        self.assertTrue(trader._profit_lock_active())

    def test_position_profit_protection_uses_wider_atr_or_percent_trail(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        trader = AutoTrader(client, self.report_dir)
        config = AutoTradeConfig(
            profit_protection_activation_pct=1,
            profit_protection_activation_atr=2,
            profit_trailing_atr=1.5,
            profit_trailing_min_pct=0.5,
            profit_floor_pct=0.2,
            profit_exit_confirmation_bars=2,
        )
        snapshot = Mock(atr=1.0)
        trader._entry_atrs["005930"] = 1.0

        armed = trader._profit_protection_state(
            "005930", 100, 104, intraday_bars([100] * 28 + [104, 104]),
            snapshot, config,
        )
        exit_state = trader._profit_protection_state(
            "005930", 100, 102, intraday_bars([100] * 27 + [102, 102, 102]),
            snapshot, config,
        )

        self.assertTrue(armed["armed"])
        self.assertAlmostEqual(armed["floor"], 102.5)
        self.assertTrue(exit_state["exit"])

    def test_profit_protection_config_is_validated(self):
        with self.assertRaisesRegex(ValueError, "청산 확인"):
            AutoTradeConfig(profit_exit_confirmation_bars=0).validate()

    def test_sells_after_armed_profit_floor_breaks_for_completed_bars(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{
                "pdno": "005930", "hldg_qty": "10", "pchs_avg_pric": "100",
                "prpr": "102",
            }],
            "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = intraday_bars([100] * 27 + [102, 102, 102])
        client.order.return_value = {"output": {"ODNO": "profit-exit"}}
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(("005930",), auto_discover=False)
        trader._trading_date = datetime.now(KST).date().isoformat()
        trader._starting_equity = 1_000_000
        trader._managed_positions.add("005930")
        trader._position_peaks["005930"] = 104
        trader._entry_atrs["005930"] = 1

        trader.run_once()

        self.assertEqual(trader._daily_orders[-1]["reason"], "profit_protection_exit")
        self.assertAlmostEqual(trader._daily_orders[-1]["profit_floor"], 102.5)

    def test_restores_daily_limits_and_open_positions_from_report(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        today = datetime.now(KST).date().isoformat()
        report = {
            "date": today,
            "starting_equity": 1_000_000,
            "latest_equity": 1_001_000,
            "peak_return_pct": 0.2,
            "orders": [
                {"time": f"{today}T10:00:00+09:00", "symbol": "005930", "side": "buy"},
                {"time": f"{today}T10:10:00+09:00", "symbol": "005930", "side": "sell"},
                {"time": f"{today}T11:00:00+09:00", "symbol": "000660", "side": "buy"},
            ],
            "executions": [],
            "catastrophe_stops": {"000660": 95_000},
            "position_peaks": {"000660": 110_000},
            "entry_atrs": {"000660": 2_500},
            "profit_floors": {"000660": 106_250},
        }
        with tempfile.TemporaryDirectory() as directory:
            report_dir = Path(directory)
            (report_dir / f"{today}.json").write_text(
                json.dumps(report), encoding="utf-8",
            )
            trader = AutoTrader(client, report_dir)
            trader._restore_daily_report()

        self.assertEqual(trader._closed_trades, 1)
        self.assertEqual(trader._entry_counts, {"005930": 1, "000660": 1})
        self.assertEqual(trader._managed_positions, {"000660"})
        self.assertEqual(trader._catastrophe_stops, {"000660": 95_000})
        self.assertEqual(trader._position_peaks, {"000660": 110_000})
        self.assertEqual(trader._entry_atrs, {"000660": 2_500})
        self.assertEqual(trader._profit_floors, {"000660": 106_250})

    def test_structure_reversal_records_exit_reason(self):
        client = Mock()
        client.settings = Settings("key", "secret", "12345678", environment="paper")
        client.balance.return_value = {
            "output1": [{"pdno": "005930", "hldg_qty": "3", "pchs_avg_pric": "100"}],
            "output2": [{"tot_evlu_amt": "1000000"}],
        }
        client.intraday_bars.return_value = three_minute_prices(
            [90, 91, 92, 93, 94, 95, 94, 93, 92]
        )
        trader = AutoTrader(client, self.report_dir)
        trader._config = AutoTradeConfig(
            ("005930",), auto_discover=False, catastrophe_atr_multiple=20,
        )
        trader._managed_positions.add("005930")
        trader._trading_date = datetime.now(KST).date().isoformat()
        trader._starting_equity = 1_000_000

        trader.run_once()

        order = client.order.call_args.args[0]
        self.assertEqual((order.side, order.quantity), ("sell", 3))
        self.assertEqual(trader._daily_orders[-1]["reason"], "structure_reversal")


if __name__ == "__main__":
    unittest.main()
