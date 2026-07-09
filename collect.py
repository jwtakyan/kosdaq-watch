# -*- coding: utf-8 -*-
"""코스닥 상장폐지 리스크 모니터링 데이터 수집기.

시세 소스 (둘 중 자동 선택):
  1) pykrx (KRX)  — KRX_ID/KRX_PW 환경변수 설정 시. 시가총액 이력까지 정확.
     ※ KRX 정보데이터시스템이 2025년부터 로그인(무료 계정)을 요구함.
  2) 네이버 금융  — 인증 불필요 fallback. 시총 이력은 (현재시총×종가비율) 근사.

재무 소스: OpenDART 사업보고서 (DART_API_KEY 설정 시) — 매출·영업이익·부채비율·자본잠식

출력: docs/data.json
캐시: dart_cache.json (확정된 연간 재무는 재요청하지 않음)

환경변수:
  DART_API_KEY   : OpenDART 인증키 (없으면 재무 항목은 비운 채 진행)
  KRX_ID, KRX_PW : KRX 정보데이터시스템 계정 (없으면 네이버 소스 사용)
  TICKER_LIMIT   : 테스트용 — 처리할 종목 수 제한 (예: 10)
"""
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET

import requests

MCAP_LIMIT = 300  # 억 원 — 스크리닝 기준 ('27.1월 시행 기준 선반영)
MCAP_RULE_NOW = 200  # 억 원 — 현행 관리종목 지정 기준
PENNY_PRICE = 1000  # 원
LOOKBACK_TRADING_DAYS = 130  # 연속일수 계산용 시세 조회 기간(거래일)
FIN_YEARS = [2023, 2024, 2025]

DART_KEY = os.environ.get("DART_API_KEY", "").strip()
USE_KRX = bool(os.environ.get("KRX_ID") and os.environ.get("KRX_PW"))
TICKER_LIMIT = int(os.environ.get("TICKER_LIMIT", "0") or 0)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(BASE_DIR, "dart_cache.json")
OUT_PATH = os.path.join(BASE_DIR, "docs", "data.json")

EOK = 100_000_000  # 1억
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def log(msg):
    print(msg, flush=True)


def streak(values, pred):
    """뒤에서부터 pred를 만족하는 연속 개수."""
    n = 0
    for v in reversed(values):
        if pred(v):
            n += 1
        else:
            break
    return n


# ---------------------------------------------------------------- 시세: pykrx (KRX)

def universe_krx():
    from pykrx import stock
    d = dt.date.today()
    for _ in range(10):
        ymd = d.strftime("%Y%m%d")
        try:
            df = stock.get_market_cap_by_ticker(ymd, market="KOSDAQ")
        except Exception:
            df = None
        if df is not None and not df.empty and int(df["시가총액"].sum()) > 0:
            rows = []
            for t, row in df.iterrows():
                rows.append({
                    "code": t,
                    "name": stock.get_market_ticker_name(t),
                    "close": int(row["종가"]),
                    "mcap": round(row["시가총액"] / EOK, 1),
                })
            return ymd, rows
        d -= dt.timedelta(days=1)
    raise RuntimeError("최근 10일 내 코스닥 시세를 찾지 못했습니다")


def history_krx(code, base_ymd):
    """(종가 리스트, 시총(억) 리스트) — 과거→현재 순."""
    from pykrx import stock
    frm = (dt.datetime.strptime(base_ymd, "%Y%m%d")
           - dt.timedelta(days=LOOKBACK_TRADING_DAYS * 2)).strftime("%Y%m%d")
    df = stock.get_market_cap(frm, base_ymd, code)
    if df is None or df.empty:
        return [], []
    return df["종가"].tolist(), [m / EOK for m in df["시가총액"].tolist()]


# ---------------------------------------------------------------- 시세: 네이버 금융

def _num(s):
    s = str(s).replace(",", "").strip()
    return float(s) if s and s != "-" else 0.0


def universe_naver():
    """코스닥 전 종목 (시총 억원). marketValue 오름차순 아님 — 전체 페이지 수집."""
    rows, page = [], 1
    while True:
        r = requests.get(
            f"https://m.stock.naver.com/api/stocks/marketValue/KOSDAQ",
            params={"page": page, "pageSize": 100}, headers=UA, timeout=30)
        r.raise_for_status()
        data = r.json()
        for s in data.get("stocks", []):
            rows.append({
                "code": s["itemCode"],
                "name": s["stockName"],
                "close": int(_num(s["closePrice"])),
                "mcap": _num(s["marketValue"]),  # 이미 억 원 단위
            })
        total = data.get("totalCount", 0)
        if page * 100 >= total or not data.get("stocks"):
            break
        page += 1
        time.sleep(0.1)
    base_ymd = dt.date.today().strftime("%Y%m%d")
    return base_ymd, rows


def history_naver(code, cur_close, cur_mcap):
    """fchart 일봉으로 종가 이력 조회. 시총 이력은 종가 비율로 근사."""
    r = requests.get(
        "https://fchart.stock.naver.com/sise.nhn",
        params={"symbol": code, "timeframe": "day",
                "count": LOOKBACK_TRADING_DAYS, "requestType": 0},
        headers=UA, timeout=30)
    r.raise_for_status()
    closes, last_ymd = [], None
    for m in re.finditer(r'data="([^"]+)"', r.text):
        parts = m.group(1).split("|")
        if len(parts) >= 5:
            last_ymd = parts[0]
            closes.append(int(_num(parts[4])))
    if not closes or cur_close <= 0:
        return [], [], last_ymd
    mcaps = [cur_mcap * c / cur_close for c in closes]
    return closes, mcaps, last_ymd


# ---------------------------------------------------------------- OpenDART

def dart_corp_map():
    """종목코드 → DART corp_code 매핑."""
    r = requests.get(
        "https://opendart.fss.or.kr/api/corpCode.xml",
        params={"crtfc_key": DART_KEY}, timeout=60)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(zf.read(zf.namelist()[0]))
    mapping = {}
    for el in root.iter("list"):
        stock_code = (el.findtext("stock_code") or "").strip()
        if stock_code:
            mapping[stock_code] = el.findtext("corp_code").strip()
    return mapping


def dart_fin_year(corp_code, year):
    """사업보고서 주요계정. 연결(CFS) 우선, 없으면 별도(OFS)."""
    r = requests.get(
        "https://opendart.fss.or.kr/api/fnlttSinglAcnt.json",
        params={"crtfc_key": DART_KEY, "corp_code": corp_code,
                "bsns_year": str(year), "reprt_code": "11011"},
        timeout=30)
    r.raise_for_status()
    data = r.json()
    if data.get("status") != "000":
        return None

    def amount(txt):
        txt = (txt or "").replace(",", "").strip()
        if not txt or txt == "-":
            return None
        try:
            return round(int(txt) / EOK, 1)
        except ValueError:
            return None

    by_fs = {"CFS": {}, "OFS": {}}
    for item in data.get("list", []):
        acc = by_fs.get(item.get("fs_div"))
        if acc is not None:
            acc[item.get("account_nm", "").strip()] = amount(item.get("thstrm_amount"))

    for fs in ("CFS", "OFS"):
        acc = by_fs[fs]
        if not acc:
            continue
        if acc.get("매출액") is None and acc.get("영업이익") is None \
                and acc.get("자산총계") is None:
            continue
        return {
            "rev": acc.get("매출액"),
            "op": acc.get("영업이익"),
            "ni": acc.get("당기순이익"),
            "assets": acc.get("자산총계"),
            "liab": acc.get("부채총계"),
            "equity": acc.get("자본총계"),
            "fs": fs,
        }
    return None


# ---------------------------------------------------------------- main

def main():
    source = "KRX(pykrx)" if USE_KRX else "네이버 금융(fallback)"
    log(f"시세 소스: {source}")

    if USE_KRX:
        base_ymd, rows = universe_krx()
    else:
        base_ymd, rows = universe_naver()
    log(f"코스닥 {len(rows)}종목 수집")

    # 보통주만(종목코드 끝자리 0) — 우선주 제외, SPAC('기업인수목적'/'스팩') 제외
    under = [r for r in rows if 0 < r["mcap"] < MCAP_LIMIT
             and r["code"].endswith("0")
             and "기업인수목적" not in r["name"]
             and "스팩" not in r["name"]]
    under.sort(key=lambda r: r["mcap"])
    log(f"시총 {MCAP_LIMIT}억 미만 (SPAC 제외): {len(under)}종목")

    if TICKER_LIMIT:
        under = under[:TICKER_LIMIT]
        log(f"TICKER_LIMIT={TICKER_LIMIT} 적용")

    cache = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)

    corp_map = {}
    if DART_KEY:
        try:
            corp_map = dart_corp_map()
            log(f"DART corp_code {len(corp_map)}건 로드")
        except Exception as e:
            log(f"[경고] DART corp_code 로드 실패: {e}")
    else:
        log("[경고] DART_API_KEY 미설정 — 재무 항목 없이 진행")

    companies = []
    for i, r in enumerate(under, 1):
        code = r["code"]
        try:
            if USE_KRX:
                closes, mcaps = history_krx(code, base_ymd)
            else:
                closes, mcaps, last_ymd = history_naver(code, r["close"], r["mcap"])
                if last_ymd:
                    base_ymd = max(base_ymd, last_ymd) if i == 1 else base_ymd
        except Exception as e:
            log(f"[경고] {code} 시세 이력 실패: {e}")
            closes, mcaps = [], []
        time.sleep(0.15)

        penny = streak(closes, lambda c: 0 < c < PENNY_PRICE)
        under300 = streak(mcaps, lambda m: 0 < m < MCAP_LIMIT)
        under200 = streak(mcaps, lambda m: 0 < m < MCAP_RULE_NOW)

        fin = {}
        debt_ratio = None
        equity_impaired = None
        corp_code = corp_map.get(code)
        if corp_code:
            for year in FIN_YEARS:
                key = f"{corp_code}:{year}"
                if key in cache:
                    fy = cache[key]
                else:
                    try:
                        fy = dart_fin_year(corp_code, year)
                    except Exception as e:
                        log(f"[경고] {code}/{year} DART 실패: {e}")
                        fy = None
                    # 과거 연도는 빈 응답도 캐시, 최신 연도 미공시만 다음 실행에서 재시도
                    if fy is not None or year < max(FIN_YEARS):
                        cache[key] = fy
                    time.sleep(0.12)
                if fy:
                    fin[str(year)] = fy
            latest = next((fin[str(y)] for y in sorted(FIN_YEARS, reverse=True)
                           if str(y) in fin), None)
            if latest and latest.get("equity") is not None:
                eq = latest["equity"]
                equity_impaired = eq <= 0
                if eq > 0 and latest.get("liab") is not None:
                    debt_ratio = round(latest["liab"] / eq * 100, 1)

        companies.append({
            "code": code,
            "name": r["name"],
            "close": r["close"],
            "mcap": round(r["mcap"], 1),
            "penny_streak": penny,
            "under300_streak": under300,
            "under200_streak": under200,
            "debt_ratio": debt_ratio,
            "equity_impaired": equity_impaired,
            "fin": fin,
        })
        if i % 25 == 0:
            log(f"  {i}/{len(under)} 처리")

    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)

    out = {
        "updated_kst": (dt.datetime.now(dt.timezone.utc)
                        + dt.timedelta(hours=9)).strftime("%Y-%m-%d %H:%M"),
        "base_date": base_ymd,
        "source": source,
        "mcap_limit": MCAP_LIMIT,
        "fin_years": FIN_YEARS,
        "has_dart": bool(corp_map),
        "companies": companies,
    }
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    log(f"완료: {OUT_PATH} ({len(companies)}종목)")


if __name__ == "__main__":
    sys.exit(main())
