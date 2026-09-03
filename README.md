# ArcTrade

ArcTrade는 자연어로 한국 주식 전략을 설계하고 백테스트한 뒤 Auto-folio에 연결하는 실험용 자동매매 도구입니다. OpenAI, Gemini, DeepSeek 또는 로컬 OpenAI 호환 모델 가운데 하나를 골라 쓸 수 있습니다. 재무 지표와 수급, 이동평균, RSI, MACD, 캔들, 공시 조건을 한 전략 안에서 함께 다룹니다.

> 이 프로젝트는 투자 자문이 아닙니다. 공개 배포본은 `AUTOFOLIO_LIVE_ORDERS=0`이 기본이며, 전략을 활성화해도 먼저 로컬 모의 장부로 실행됩니다.

## 주요 기능

- 자연어 아이디어를 실행 가능한 전략 JSON으로 변환
- AI 전략 생성, 백테스트, 전략 활성화를 각각 확인할 수 있는 3개 버튼
- AND, OR, 괄호를 이용한 복합 진입·청산 조건과 여러 자산 배분 방식
- 거래비용을 반영한 일별 백테스트와 KOSPI·Buy & Hold 비교
- 활성 전략의 운용 시간, 수익률, 신호 수를 실시간 기록
- 전략 교체 전 경고창과 기존 실험 이력 자동 보관
- 전략을 바꾸면 전략 수익률·신호 로그·모의 포지션만 초기화
- Auto-folio의 타임폴리오 가격, 보유 내역, 주문 원장은 그대로 보존
- 타임폴리오 계정과 모든 API 키를 환경변수로 주입

과매도 s-score 화면은 제거했습니다. 해당 연구 코드는 재현을 위해 남겨 두었지만 새 대시보드와 AI 전략 운용 흐름에서는 쓰지 않습니다.

## 빠른 시작

Python 3.11 이상을 권장합니다.

```bash
git clone https://github.com/Arcosium/ArcTrade.git
cd ArcTrade
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
```

`.env`에 사용할 AI 제공사의 키 하나만 입력합니다. 여러 키를 넣고 `LLM_PROVIDER=auto`로 두면 OpenAI, Gemini, DeepSeek 순서로 사용 가능한 제공사를 고릅니다. 키 없이 로컬 모델을 쓰려면 `LLM_PROVIDER=local`과 `LOCAL_LLM_BASE_URL`을 설정합니다.

환경변수를 적용한 뒤 서버를 실행합니다.

```bash
set -a
source .env
set +a
python -m web.app
```

브라우저에서 `http://127.0.0.1:8620`을 엽니다.

## AI 제공사 설정

키 값은 브라우저 입력창이나 저장소에 저장하지 않습니다. 서버 프로세스의 환경변수에서만 읽습니다.

```dotenv
OPENAI_API_KEY=""
GEMINI_API_KEY=""
DEEPSEEK_API_KEY=""
```

각 제공사의 모델과 기본 URL도 바꿀 수 있습니다. Gemini와 DeepSeek는 공식 OpenAI 호환 Chat Completions 엔드포인트를 사용합니다. 사내 게이트웨이나 프록시가 있다면 해당 `*_BASE_URL`을 지정하면 됩니다.

## 타임폴리오 연결

타임폴리오 계정은 소스 코드에 넣지 않습니다.

```dotenv
TIMEFOLIO_USERNAME=""
TIMEFOLIO_PASSWORD=""
AUTOFOLIO_LIVE_ORDERS=0
```

로그인이 확인되고 주문 위험을 이해한 뒤에만 `AUTOFOLIO_LIVE_ORDERS=1`로 바꾸십시오. 이 값이 0이면 Auto-folio는 로컬 모의 장부를 사용합니다. 계정 정보는 런타임 저장소에서 암호화되며 Git 추적 대상이 아닙니다.

## 전략 연구 흐름

전략 연구실에서 아이디어를 적고 `AI로 전략 생성`을 누릅니다. 생성된 재무 필터, 매수·매도 조건, 검증 기간, 배분 방식을 확인한 다음 `백테스트`를 실행합니다. 백테스트를 마친 전략만 `이 전략으로 매매하기`로 활성화할 수 있습니다.

이미 다른 전략이 운용 중이면 ArcTrade가 수익률과 신호 로그를 초기화할지 묻습니다. 확인하면 기존 기록을 `strategy_archives`에 보관하고 새 전략을 0%부터 추적합니다. Auto-folio의 실제 계정 장부는 이 과정에 포함되지 않습니다.

브라우저마다 무작위 프로필 ID를 만들기 때문에 실험 기록이 서로 섞이지 않습니다. 프로필 ID에는 이름이나 이메일을 넣지 않습니다. 기록 파일은 기본적으로 `~/.local/share/arctrade/profiles`에 저장됩니다.

## 데이터와 비밀 관리

기본 런타임 경로는 저장소 밖인 `~/.local/share/arctrade`입니다. 다음 파일은 커밋하지 않습니다.

- `.env`와 API 키, 타임폴리오 아이디·비밀번호
- 사용자별 실험 기록과 Auto-folio 상태
- SQLite DB, 신호 로그, 거래 로그, 브라우저 세션
- 시세 캐시와 Parquet 파일

경로를 바꾸려면 `ARCTRADE_PRIVATE_DATA_DIR`, `ARCTRADE_DATA_DIR`, `ARCTRADE_MARKET_DATA_DIR`, `ARCTRADE_QUANT_CACHE_DIR`을 지정합니다.

## 지원하는 전략 표현

재무 필터에는 PER, PBR, ROE, 부채비율, 성장률 등을 쓸 수 있습니다. 매수·매도 조건에는 이동평균선, RSI, MACD, 스토캐스틱, 거래대금, 외국인·기관·개인 순매수, 캔들 패턴, DART 공시 이벤트를 조합할 수 있습니다.

포트폴리오 방식은 동일비중, 시가총액, 모멘텀, 위험균형, 역변동성, Kelly 근사, 최소분산 근사, 최대 Sharpe 근사, 동적 배분, 단일 종목 집중을 지원합니다. 일부 방식은 휴리스틱 근사이므로 결과를 실제 운용 전에 별도로 검증해야 합니다.

## 테스트

```bash
pytest -q
python -m web.nlbacktest
```

웹 화면은 1440×900과 390×844에서 확인합니다. 공개 전에는 아래 명령으로 추적 대상에 비밀이 없는지 다시 검사하는 편이 안전합니다.

```bash
git grep -nEi '(api[_-]?key|password|secret|token|@gmail\.com)'
```

변수명과 빈 예시는 검색되더라도 실제 값은 없어야 합니다.

## 구조

```text
ArcTrade/
├── web/               FastAPI 서버, AI 제공사 연결, 실험 기록, 대시보드
├── quant/             자연어 조건 백테스트 엔진과 재무 데이터 갱신
├── core/              분봉 연구 엔진과 통계 모듈
├── Auto_folio/        타임폴리오 모의·사이트 주문 어댑터
├── crypto/            별도 암호자산 연구 코드
├── tests/             핵심 전략·주문 로직 테스트
├── .env.example       비밀이 없는 설정 예시
└── requirements.txt   Python 의존성
```

MIT License로 공개합니다.
