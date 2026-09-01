from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import threading
import time
from typing import Any

import requests

from .config import Settings


class KISAPIError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    price: int = 0
    order_type: str = "market"


class KISClient:
    def __init__(self, settings: Settings, session: requests.Session | None = None):
        self.settings = settings
        self.session = session or requests.Session()
        self._token = ""
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._rate_lock = threading.Lock()
        self._last_request_at = 0.0

    def _throttle(self) -> None:
        minimum_interval = 1.1 if self.settings.environment == "paper" else 0.15
        with self._rate_lock:
            wait = minimum_interval - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()

    def authenticate(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now < self._token_expires_at:
            return self._token
        self._throttle()
        response = self.session.post(
            self.settings.base_url + "/oauth2/tokenP",
            json={
                "grant_type": "client_credentials",
                "appkey": self.settings.app_key,
                "appsecret": self.settings.app_secret,
            },
            timeout=self.settings.timeout_seconds,
        )
        data = self._decode(response)
        self._token = data.get("access_token", "")
        if not self._token:
            raise KISAPIError("인증 응답에 access_token이 없습니다.")
        seconds = max(60, int(data.get("expires_in", 86400)) - 60)
        self._token_expires_at = now + timedelta(seconds=seconds)
        return self._token

    def _headers(
        self, tr_id: str, *, hashkey: str = "", tr_cont: str = ""
    ) -> dict[str, str]:
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {self.authenticate()}",
            "appkey": self.settings.app_key,
            "appsecret": self.settings.app_secret,
            "tr_id": tr_id,
            "custtype": "P",
        }
        if hashkey:
            headers["hashkey"] = hashkey
        if tr_cont:
            headers["tr_cont"] = tr_cont
        return headers

    @staticmethod
    def _decode(response: requests.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise KISAPIError(f"KIS API가 JSON이 아닌 응답을 반환했습니다 (HTTP {response.status_code}).") from exc
        if not response.ok:
            raise KISAPIError(f"KIS API HTTP {response.status_code}: {data.get('msg1', data)}")
        if "rt_cd" in data and data["rt_cd"] != "0":
            raise KISAPIError(f"KIS API {data.get('msg_cd', 'error')}: {data.get('msg1', '요청 실패')}")
        return data

    def _get(self, path: str, tr_id: str, params: dict[str, str]) -> dict[str, Any]:
        _, data = self._get_response(path, tr_id, params)
        return data

    def _get_response(
        self, path: str, tr_id: str, params: dict[str, str], *, tr_cont: str = ""
    ) -> tuple[requests.Response, dict[str, Any]]:
        for attempt in range(3):
            self._throttle()
            response = self.session.get(
                self.settings.base_url + path,
                headers=self._headers(tr_id, tr_cont=tr_cont),
                params=params,
                timeout=self.settings.timeout_seconds,
            )
            try:
                return response, self._decode(response)
            except KISAPIError as exc:
                if attempt == 2 or ("EGW00201" not in str(exc) and "초당 거래건수" not in str(exc)):
                    raise
                time.sleep(2.0 * (attempt + 1))
        raise KISAPIError("KIS API 호출 재시도에 실패했습니다.")

    def _hashkey(self, body: dict[str, str]) -> str:
        self._throttle()
        response = self.session.post(
            self.settings.base_url + "/uapi/hashkey",
            headers={
                "content-type": "application/json; charset=utf-8",
                "appkey": self.settings.app_key,
                "appsecret": self.settings.app_secret,
            },
            json=body,
            timeout=self.settings.timeout_seconds,
        )
        return str(self._decode(response).get("HASH", ""))

    def current_price(self, symbol: str) -> int:
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-price",
            "FHKST01010100",
            {"FID_COND_MRKT_DIV_CODE": "J", "FID_INPUT_ISCD": symbol},
        )
        return int(data["output"]["stck_prpr"])

    def daily_prices(self, symbol: str, period: str = "D") -> list[int]:
        end = date.today()
        start = end - timedelta(days=120)
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-daily-itemchartprice",
            "FHKST03010100",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "1",
            },
        )
        rows = data.get("output2", [])
        return [int(row["stck_clpr"]) for row in reversed(rows) if row.get("stck_clpr")]

    def intraday_bars(self, symbol: str) -> list[dict[str, int | str]]:
        now_kst = datetime.now(timezone(timedelta(hours=9)))
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice",
            "FHKST03010200",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_INPUT_ISCD": symbol,
                "FID_INPUT_HOUR_1": now_kst.strftime("%H%M%S"),
                "FID_PW_DATA_INCU_YN": "Y",
                "FID_ETC_CLS_CODE": "",
            },
        )
        rows = sorted(data.get("output2") or [], key=lambda row: row.get("stck_cntg_hour", ""))
        return [
            {
                "time": str(row.get("stck_cntg_hour") or ""),
                "price": int(row.get("stck_prpr") or 0),
                "volume": int(row.get("cntg_vol") or 0),
            }
            for row in rows if row.get("stck_prpr")
        ]

    def intraday_prices(self, symbol: str) -> list[int]:
        return [int(bar["price"]) for bar in self.intraday_bars(symbol)]

    def volume_rank(self, *, limit: int = 10, division: str = "1") -> list[dict[str, Any]]:
        data = self._get(
            "/uapi/domestic-stock/v1/quotations/volume-rank",
            "FHPST01710000",
            {
                "FID_COND_MRKT_DIV_CODE": "J",
                "FID_COND_SCR_DIV_CODE": "20171",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": division,
                "FID_BLNG_CLS_CODE": "3",
                "FID_TRGT_CLS_CODE": "111111111",
                "FID_TRGT_EXLS_CLS_CODE": "0000000000",
                "FID_INPUT_PRICE_1": "1000",
                "FID_INPUT_PRICE_2": "500000",
                "FID_VOL_CNT": "100000",
                "FID_INPUT_DATE_1": "",
            },
        )
        return (data.get("output") or [])[:max(1, min(limit, 30))]

    def daytrade_rank(self, *, limit: int = 10) -> list[dict[str, Any]]:
        ordinary = self.volume_rank(limit=min(limit, 30), division="1")
        etf_prefixes = (
            "ACE ", "ARIRANG ", "HANARO ", "KODEX ", "KOSEF ", "PLUS ",
            "RISE ", "SOL ", "TIGER ", "TIMEFOLIO ",
        )
        unique: dict[str, dict[str, Any]] = {}
        for row in ordinary:
            symbol = str(row.get("mksc_shrn_iscd") or row.get("stck_shrn_iscd") or "")
            name = str(row.get("hts_kor_isnm") or "").upper()
            if symbol and not name.startswith(etf_prefixes):
                unique[symbol] = row
        ranked = sorted(
            unique.values(),
            key=lambda row: int(row.get("acml_tr_pbmn") or 0),
            reverse=True,
        )
        return ranked[:max(1, min(limit, 20))]

    def balance(self) -> dict[str, Any]:
        tr_id = "VTTC8434R" if self.settings.environment == "paper" else "TTTC8434R"
        path = "/uapi/domestic-stock/v1/trading/inquire-balance"
        params = {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "AFHR_FLPR_YN": "N",
                "OFL_YN": "",
                "INQR_DVSN": "02",
                "UNPR_DVSN": "01",
                "FUND_STTL_ICLD_YN": "N",
                "FNCG_AMT_AUTO_RDPT_YN": "N",
                "PRCS_DVSN": "00",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
        }
        holdings: list[dict[str, Any]] = []
        summary: list[dict[str, Any]] = []
        tr_cont = ""

        for _ in range(10):
            response, data = self._get_response(path, tr_id, params, tr_cont=tr_cont)
            holdings.extend(data.get("output1") or [])
            if not summary:
                summary = data.get("output2") or []

            tr_cont = response.headers.get("tr_cont", "")
            if tr_cont not in {"M", "F"}:
                break
            next_fk = data.get("ctx_area_fk100", "")
            next_nk = data.get("ctx_area_nk100", "")
            if not next_fk and not next_nk:
                break
            params["CTX_AREA_FK100"] = next_fk
            params["CTX_AREA_NK100"] = next_nk

        return {"output1": holdings, "output2": summary}

    def daily_executions(self, date: str) -> list[dict[str, Any]]:
        """Return today's order/fill rows for reporting without account identifiers."""
        compact_date = date.replace("-", "")
        tr_id = "VTTC0081R" if self.settings.environment == "paper" else "TTTC0081R"
        data = self._get(
            "/uapi/domestic-stock/v1/trading/inquire-daily-ccld",
            tr_id,
            {
                "CANO": self.settings.account_no,
                "ACNT_PRDT_CD": self.settings.product_code,
                "INQR_STRT_DT": compact_date,
                "INQR_END_DT": compact_date,
                "SLL_BUY_DVSN_CD": "00",
                "PDNO": "",
                "CCLD_DVSN": "01",
                "INQR_DVSN": "01",
                "INQR_DVSN_3": "00",
                "ORD_GNO_BRNO": "",
                "ODNO": "",
                "INQR_DVSN_1": "",
                "CTX_AREA_FK100": "",
                "CTX_AREA_NK100": "",
            },
        )
        fields = (
            "ord_dt", "ord_tmd", "odno", "pdno", "prdt_name",
            "sll_buy_dvsn_cd_name", "ord_qty", "tot_ccld_qty",
            "avg_prvs", "tot_ccld_amt", "rmn_qty",
        )
        return [
            {field: row.get(field, "") for field in fields}
            for row in (data.get("output1") or [])
        ]

    def order(self, request: OrderRequest) -> dict[str, Any]:
        self._validate_order(request)
        buy = request.side == "buy"
        tr_ids = {
            ("paper", True): "VTTC0802U", ("paper", False): "VTTC0801U",
            ("live", True): "TTTC0802U", ("live", False): "TTTC0801U",
        }
        body = {
            "CANO": self.settings.account_no,
            "ACNT_PRDT_CD": self.settings.product_code,
            "PDNO": request.symbol,
            "ORD_DVSN": "01" if request.order_type == "market" else "00",
            "ORD_QTY": str(request.quantity),
            "ORD_UNPR": "0" if request.order_type == "market" else str(request.price),
        }
        self._throttle()
        response = self.session.post(
            self.settings.base_url + "/uapi/domestic-stock/v1/trading/order-cash",
            headers=self._headers(tr_ids[(self.settings.environment, buy)], hashkey=self._hashkey(body)),
            json=body,
            timeout=self.settings.timeout_seconds,
        )
        return self._decode(response)

    def _validate_order(self, request: OrderRequest) -> None:
        if request.side not in {"buy", "sell"}:
            raise ValueError("side는 buy 또는 sell이어야 합니다.")
        if request.order_type not in {"market", "limit"}:
            raise ValueError("order_type은 market 또는 limit이어야 합니다.")
        if request.quantity <= 0 or (request.order_type == "limit" and request.price <= 0):
            raise ValueError("수량과 지정가는 양수여야 합니다.")
        if self.settings.allowed_symbols and request.symbol not in self.settings.allowed_symbols:
            raise ValueError(f"허용되지 않은 종목입니다: {request.symbol}")
        reference_price = request.price or self.current_price(request.symbol)
        if reference_price * request.quantity > self.settings.max_order_krw:
            raise ValueError(
                f"주문금액 {reference_price * request.quantity:,}원이 "
                f"한도 {self.settings.max_order_krw:,}원을 초과합니다."
            )
        if self.settings.environment == "live" and not self.settings.allow_live:
            raise ValueError("실전 주문이 잠겨 있습니다. STOCKCAST_ALLOW_LIVE=true가 필요합니다.")
