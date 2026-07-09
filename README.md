# 코스닥 시총 300억 미만 모니터

코스닥 상장폐지 요건 강화(시총·동전주·자본잠식)에 대응해, 시가총액 300억 미만 종목을
매 영업일 자동 수집·스크리닝하는 정적 웹 대시보드입니다.

- **시세**: KRX (pykrx) 또는 네이버 금융(무인증 fallback) — 시가총액, 주가, 동전주/시총미달 연속일수
  - KRX 정보데이터시스템이 2025년부터 로그인(무료 계정)을 요구하여, `KRX_ID`/`KRX_PW` 시크릿이
    설정된 경우에만 pykrx 사용. 미설정 시 네이버 금융 API로 자동 전환(시총 이력은 종가비율 근사).
- **재무**: 금융감독원 OpenDART — 매출액, 영업이익, 부채비율, 자본잠식 (사업보고서, 연결 우선)
- **자동화**: GitHub Actions가 평일 17:40 KST에 `collect.py` 실행 → `docs/data.json` 커밋
- **웹**: GitHub Pages (`docs/` 폴더)

## 설정

1. **DART API 키** (재무 데이터용): [opendart.fss.or.kr](https://opendart.fss.or.kr) 가입 → 인증키 발급
   → 저장소 Settings → Secrets and variables → Actions → `DART_API_KEY` 등록
2. **KRX 계정** (선택, 정확한 시총 이력용): [data.krx.co.kr](https://data.krx.co.kr) 무료 가입
   → 시크릿 `KRX_ID`, `KRX_PW` 등록. 미등록 시 네이버 금융 소스로 동작.
3. **GitHub Pages**: Settings → Pages → Branch `main` / 폴더 `/docs`

## 로컬 실행

```bash
pip install -r requirements.txt
set DART_API_KEY=발급받은키          # PowerShell: $env:DART_API_KEY="..."
python collect.py
# docs/index.html 을 브라우저로 열기 (또는: python -m http.server -d docs)
```

`TICKER_LIMIT=10` 환경변수로 소수 종목만 테스트할 수 있습니다.

## 판정 기준 (2026.7 시행 규정 반영)

| 항목 | 기준 |
|---|---|
| 시가총액 | 현행 200억 미만 관리종목 → '27.1월부터 300억 (스크리닝은 300억 선반영) |
| 동전주 | 주가 1,000원 미만 30거래일 연속 시 관리종목 지정 |
| 완전자본잠식 | 자본총계 ≤ 0 (반기 기준 확대) |

⚠️ 본 자료는 참고용이며 투자 판단에 대한 책임은 이용자에게 있습니다.
