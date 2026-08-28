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


if __name__ == "__main__":
    unittest.main()
