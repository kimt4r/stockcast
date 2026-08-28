from __future__ import annotations

import argparse
import json
import sys

import requests

from .config import Settings
from .kis import KISAPIError, KISClient, OrderRequest
from .strategy import SmaCrossStrategy


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="한국투자증권 Open API 자동매매 CLI")
    sub = root.add_subparsers(dest="command", required=True)
    quote = sub.add_parser("quote", help="현재가 조회")
    quote.add_argument("symbol")
    sub.add_parser("balance", help="계좌 잔고 조회")
    signal = sub.add_parser("signal", help="이동평균 교차 신호 계산")
    signal.add_argument("symbol")
    signal.add_argument("--short", type=int, default=5)
    signal.add_argument("--long", type=int, default=20)
    order = sub.add_parser("order", help="현금 주문 (기본은 미리보기)")
    order.add_argument("side", choices=["buy", "sell"])
    order.add_argument("symbol")
    order.add_argument("quantity", type=int)
    order.add_argument("--price", type=int, default=0, help="생략 시 시장가")
    order.add_argument("--execute", action="store_true", help="실제로 API 주문 전송")
    order.add_argument("--live-confirm", default="", help="실전 주문 시 LIVE 입력")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        settings = Settings.from_env()
        client = KISClient(settings)
        if args.command == "quote":
            print(f"{args.symbol}: {client.current_price(args.symbol):,}원")
        elif args.command == "balance":
            print(json.dumps(client.balance(), ensure_ascii=False, indent=2))
        elif args.command == "signal":
            closes = client.daily_prices(args.symbol)
            result = SmaCrossStrategy(args.short, args.long).evaluate(closes)
            print(f"{args.symbol}: {result.value} (종가 {len(closes)}개)")
        elif args.command == "order":
            request = OrderRequest(
                symbol=args.symbol,
                side=args.side,
                quantity=args.quantity,
                price=args.price,
                order_type="limit" if args.price else "market",
            )
            client._validate_order(request)
            summary = f"{settings.environment} {args.side} {args.symbol} {args.quantity}주"
            if args.price:
                summary += f" @{args.price:,}원"
            if not args.execute:
                print(f"DRY-RUN: {summary} (전송하려면 --execute)")
                return 0
            if settings.environment == "live" and args.live_confirm != "LIVE":
                raise ValueError("실전 주문에는 --live-confirm LIVE가 필요합니다.")
            print(json.dumps(client.order(request), ensure_ascii=False, indent=2))
        return 0
    except (ValueError, KISAPIError, KeyError, requests.RequestException) as exc:
        print(f"오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
