# Stockcast

한국투자증권(KIS) Open API를 이용하는 국내주식 자동매매의 최소 기반입니다. 현재가, 일봉,
잔고, 현금 매수·매도와 단순 이동평균 교차 신호를 제공합니다. 기본 환경은 **모의투자**이고,
주문 명령도 `--execute` 없이는 API로 전송되지 않습니다.

> 이 코드는 투자 수익을 보장하지 않습니다. 반드시 모의투자에서 충분히 검증하고 API 호출
> 제한, 거래 시간, 호가 단위, 세금·수수료를 별도로 확인하세요.

## 준비

1. [KIS Developers](https://apiportal.koreainvestment.com/)에서 Open API를 신청하고 모의투자 앱 키와 계좌를 준비합니다.
2. Python 3.11 이상에서 가상환경을 만들고 설치합니다.

```powershell
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e .
Copy-Item .env.example .env
```

`.env`에 모의투자용 `KIS_APP_KEY`, `KIS_APP_SECRET`, 계좌번호 앞 8자리를 입력합니다.
계좌번호 뒤 2자리는 일반 종합계좌라면 보통 `01`입니다. 키와 계좌 정보는 절대 커밋하지
마세요.

## 사용

```powershell
# 삼성전자 현재가와 계좌 잔고
stockcast quote 005930
stockcast balance

# 5일/20일 이동평균 교차 신호 (주문하지 않음)
stockcast signal 005930 --short 5 --long 20

# 주문 미리보기와 모의투자 주문 전송
stockcast order buy 005930 1
stockcast order buy 005930 1 --execute
stockcast order sell 005930 1 --price 70000 --execute
```

주문은 `.env`의 `STOCKCAST_ALLOWED_SYMBOLS`에 등록된 종목만 가능하고,
`STOCKCAST_MAX_ORDER_KRW`를 초과하면 거부됩니다. 시장가 주문 한도 검사에는 주문 직전
현재가가 사용됩니다.

실전 모드는 세 가지 조건을 모두 만족해야 합니다: `KIS_ENV=live`,
`STOCKCAST_ALLOW_LIVE=true`, 명령의 `--live-confirm LIVE`. 예:

```powershell
stockcast order buy 005930 1 --execute --live-confirm LIVE
```

## 현재 설계 범위

- `config.py`: 환경 설정과 검증
- `kis.py`: OAuth 토큰, 시세·잔고·주문 REST 클라이언트
- `strategy.py`: 종가 기준 SMA 교차 신호
- `cli.py`: 사람이 확인하며 실행하는 진입점
- `tests/`: API 모킹 및 전략·리스크 단위 테스트

자동 반복 실행은 아직 의도적으로 넣지 않았습니다. 다음 단계에서 거래 캘린더, 중복 주문
방지, 미체결/체결 조회, 포지션 크기 산정, 손절·일일손실 제한을 먼저 추가한 뒤 스케줄러를
연결하는 편이 안전합니다.

## 테스트

```powershell
python -m unittest discover -s tests -v
```

API 경로와 TR ID는 한국투자증권의 공식
[Open Trading API 샘플](https://github.com/koreainvestment/open-trading-api)을 기준으로 했습니다.

## 시각적 로컬 앱

API 키를 파일에 입력하지 않고 연결 화면에서 입력해 사용할 수도 있습니다. 연결 정보는
실행 중인 프로세스의 메모리에만 있고 쿠키에는 무작위 세션 ID만 저장됩니다. 앱을 끄거나
연결을 해제하면 자격 증명이 사라집니다.

```powershell
pip install -e .
python -m stockcast.web
```

브라우저에서 `http://127.0.0.1:8787`을 여세요. 앱은 외부 접속을 받지 않고 이 PC의
localhost에만 바인딩됩니다. 대시보드에서 현재가와 종가 차트, SMA 신호, 보유 잔고,
주문 조건 미리보기를 확인할 수 있습니다. 안전을 위해 현재 UI는 주문 전송 대신 조건
검증까지만 수행합니다.
