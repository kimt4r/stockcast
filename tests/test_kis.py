import unittest
from unittest.mock import Mock
from unittest.mock import patch

from stockcast.config import Settings
from stockcast.kis import KISClient


class KISClientTests(unittest.TestCase):
    @staticmethod
    def authenticated_client(session):
        client = KISClient(Settings("key", "secret", "12345678"), session)
        from datetime import datetime, timedelta, timezone
        client._token = "cached"
        client._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        return client

    def test_current_price(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"rt_cd": "0", "output": {"stck_prpr": "71200"}}
        session = Mock()
        session.get.return_value = response
        client = self.authenticated_client(session)

        self.assertEqual(client.current_price("005930"), 71_200)
        self.assertEqual(session.get.call_args.kwargs["headers"]["tr_id"], "FHKST01010100")

    def test_volume_rank_uses_domestic_ranking_api(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "rt_cd": "0",
            "output": [{"mksc_shrn_iscd": "005930"}, {"mksc_shrn_iscd": "000660"}],
        }
        session = Mock()
        session.get.return_value = response

        rows = self.authenticated_client(session).volume_rank(limit=1)

        self.assertEqual(rows, [{"mksc_shrn_iscd": "005930"}])
        call = session.get.call_args
        self.assertTrue(call.args[0].endswith("/quotations/volume-rank"))
        self.assertEqual(call.kwargs["headers"]["tr_id"], "FHPST01710000")
        self.assertEqual(call.kwargs["params"]["FID_BLNG_CLS_CODE"], "3")

    def test_intraday_prices_are_sorted_oldest_to_newest(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "rt_cd": "0",
            "output2": [
                {"stck_cntg_hour": "101000", "stck_prpr": "72000"},
                {"stck_cntg_hour": "100900", "stck_prpr": "71000"},
            ],
        }
        session = Mock()
        session.get.return_value = response

        prices = self.authenticated_client(session).intraday_prices("005930")

        self.assertEqual(prices, [71_000, 72_000])
        call = session.get.call_args
        self.assertTrue(call.args[0].endswith("/quotations/inquire-time-itemchartprice"))
        self.assertEqual(call.kwargs["headers"]["tr_id"], "FHKST03010200")

    def test_intraday_bars_include_ohlcv_for_structure_strategy(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "rt_cd": "0",
            "output2": [{
                "stck_cntg_hour": "101000", "stck_oprc": "71000",
                "stck_hgpr": "72000", "stck_lwpr": "70500",
                "stck_prpr": "71500", "cntg_vol": "123",
            }],
        }
        session = Mock()
        session.get.return_value = response

        bars = self.authenticated_client(session).intraday_bars("005930")

        self.assertEqual(bars[0], {
            "time": "101000", "open": 71_000, "high": 72_000,
            "low": 70_500, "price": 71_500, "volume": 123,
        })

    def test_daytrade_rank_returns_common_stocks_and_inverse_etfs(self):
        client = self.authenticated_client(Mock())
        client.volume_rank = Mock(side_effect=[
            [
                {"mksc_shrn_iscd": "005930", "hts_kor_isnm": "삼성전자", "acml_tr_pbmn": "100"},
                {"mksc_shrn_iscd": "122630", "hts_kor_isnm": "KODEX 레버리지", "acml_tr_pbmn": "600"},
                {"mksc_shrn_iscd": "360750", "hts_kor_isnm": "TIGER 미국S&P500", "acml_tr_pbmn": "550"},
            ],
            [
                {"mksc_shrn_iscd": "0193L0", "hts_kor_isnm": "PLUS 삼성전자선물단일종목인버스2X", "acml_tr_pbmn": "500"},
                {"mksc_shrn_iscd": "122630", "hts_kor_isnm": "KODEX 레버리지", "acml_tr_pbmn": "300"},
                {"mksc_shrn_iscd": "360750", "hts_kor_isnm": "TIGER 미국S&P500", "acml_tr_pbmn": "200"},
                {"mksc_shrn_iscd": "999999", "hts_kor_isnm": "삼성 인버스 ETN", "acml_tr_pbmn": "400"},
            ],
        ])

        rows = client.daytrade_rank(limit=10)

        self.assertEqual(
            [row["mksc_shrn_iscd"] for row in rows], ["0193L0", "005930"],
        )
        self.assertEqual(client.volume_rank.call_args_list, [
            unittest.mock.call(limit=30, division="1"),
            unittest.mock.call(limit=30, division="0"),
        ])

    def test_daily_prices_loads_enough_history_for_long_trend(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "rt_cd": "0",
            "output2": [{"stck_clpr": "72000"}, {"stck_clpr": "71000"}],
        }
        session = Mock()
        session.get.return_value = response

        prices = self.authenticated_client(session).daily_prices("005930")

        self.assertEqual(prices, [71_000, 72_000])
        call = session.get.call_args
        self.assertTrue(call.args[0].endswith("/quotations/inquire-daily-itemchartprice"))
        self.assertEqual(call.kwargs["headers"]["tr_id"], "FHKST03010100")
        self.assertIn("FID_INPUT_DATE_1", call.kwargs["params"])

    def test_paper_balance_follows_continuation_and_keeps_summary(self):
        first = Mock(ok=True, status_code=200)
        first.headers = {"tr_cont": "M"}
        first.json.return_value = {
            "rt_cd": "0",
            "output1": [{"pdno": "005930"}],
            "output2": [{"dnca_tot_amt": "1000000"}],
            "ctx_area_fk100": "next-fk",
            "ctx_area_nk100": "next-nk",
        }
        second = Mock(ok=True, status_code=200)
        second.headers = {"tr_cont": ""}
        second.json.return_value = {
            "rt_cd": "0",
            "output1": [{"pdno": "000660"}],
            "output2": [],
        }
        session = Mock()
        session.get.side_effect = [first, second]

        data = self.authenticated_client(session).balance()

        self.assertEqual([row["pdno"] for row in data["output1"]], ["005930", "000660"])
        self.assertEqual(data["output2"][0]["dnca_tot_amt"], "1000000")
        self.assertEqual(session.get.call_count, 2)
        second_call = session.get.call_args_list[1].kwargs
        self.assertEqual(second_call["headers"]["tr_cont"], "M")
        self.assertEqual(second_call["params"]["CTX_AREA_FK100"], "next-fk")

    def test_daily_executions_uses_paper_fill_inquiry_and_sanitizes_rows(self):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {
            "rt_cd": "0",
            "output1": [{
                "ord_dt": "20260901", "ord_tmd": "105320", "odno": "12345",
                "pdno": "005930", "prdt_name": "삼성전자",
                "sll_buy_dvsn_cd_name": "매수", "ord_qty": "3",
                "tot_ccld_qty": "3", "avg_prvs": "71200",
                "tot_ccld_amt": "213600", "rmn_qty": "0", "CANO": "secret",
            }],
        }
        session = Mock()
        session.get.return_value = response

        rows = self.authenticated_client(session).daily_executions("2026-09-01")

        self.assertEqual(rows[0]["avg_prvs"], "71200")
        self.assertNotIn("CANO", rows[0])
        call = session.get.call_args
        self.assertTrue(call.args[0].endswith("/trading/inquire-daily-ccld"))
        self.assertEqual(call.kwargs["headers"]["tr_id"], "VTTC0081R")
        self.assertEqual(call.kwargs["params"]["CCLD_DVSN"], "01")

    def test_daily_executions_follows_continuation(self):
        first = Mock(ok=True, status_code=200)
        first.headers = {"tr_cont": "M"}
        first.json.return_value = {
            "rt_cd": "0", "output1": [{"odno": "1", "pdno": "005930"}],
            "ctx_area_fk100": "next-fk", "ctx_area_nk100": "next-nk",
        }
        second = Mock(ok=True, status_code=200)
        second.headers = {"tr_cont": ""}
        second.json.return_value = {
            "rt_cd": "0", "output1": [{"odno": "2", "pdno": "000660"}],
        }
        session = Mock()
        session.get.side_effect = [first, second]

        rows = self.authenticated_client(session).daily_executions("2026-09-03")

        self.assertEqual([row["odno"] for row in rows], ["1", "2"])
        second_call = session.get.call_args_list[1].kwargs
        self.assertEqual(second_call["headers"]["tr_cont"], "M")
        self.assertEqual(second_call["params"]["CTX_AREA_FK100"], "next-fk")

    def test_empty_optional_environment_values_use_safe_defaults(self):
        environment = {
            "KIS_APP_KEY": "key",
            "KIS_APP_SECRET": "secret",
            "KIS_ACCOUNT_NO": "12345678",
            "KIS_ACCOUNT_PRODUCT_CODE": "",
            "KIS_ENV": "",
            "STOCKCAST_MAX_ORDER_KRW": "",
            "STOCKCAST_ALLOW_LIVE": "",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = Settings.from_env("file-that-does-not-exist")

        self.assertEqual(settings.environment, "paper")
        self.assertEqual(settings.product_code, "01")
        self.assertEqual(settings.max_order_krw, 100_000)
        self.assertFalse(settings.allow_live)

    def test_alphanumeric_etf_symbol_is_normalized_in_allowlist(self):
        environment = {
            "KIS_APP_KEY": "key",
            "KIS_APP_SECRET": "secret",
            "KIS_ACCOUNT_NO": "12345678",
            "STOCKCAST_ALLOWED_SYMBOLS": "0193l0",
        }
        with patch.dict("os.environ", environment, clear=True):
            settings = Settings.from_env("file-that-does-not-exist")

        self.assertEqual(settings.allowed_symbols, frozenset({"0193L0"}))


if __name__ == "__main__":
    unittest.main()
