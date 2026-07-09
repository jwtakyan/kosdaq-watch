# -*- coding: utf-8 -*-
"""주간 AI 투자 후보 추천 — OpenAI API.

docs/data.json(스크리닝 데이터)을 입력으로 AI가 PE 관점에서
투자(지분매수)·인수(경영권) 후보 5개를 선정하고,
docs/recommendations.json에 이력을 누적 저장한다.

환경변수: OPENAI_API_KEY (필수)
"""
import datetime as dt
import json
import os
import sys

from openai import OpenAI

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_PATH = os.path.join(BASE_DIR, "docs", "data.json")
REC_PATH = os.path.join(BASE_DIR, "docs", "recommendations.json")

MODEL = "gpt-5.5"

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "market_note": {
            "type": "string",
            "description": "이번 주 유니버스 전반에 대한 2~3문장 총평",
        },
        "picks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "종목코드 6자리"},
                    "name": {"type": "string"},
                    "angle": {"type": "string", "enum": ["지분투자", "인수"]},
                    "thesis": {
                        "type": "string",
                        "description": "선정 근거 2~3문장 (사업·재무·밸류에이션 관점)",
                    },
                    "key_metrics": {
                        "type": "string",
                        "description": "핵심 수치 요약 한 줄 (시총/자본/영업이익 등)",
                    },
                    "risks": {"type": "string", "description": "주요 리스크 한 줄"},
                },
                "required": ["code", "name", "angle", "thesis", "key_metrics", "risks"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["market_note", "picks"],
    "additionalProperties": False,
}

SYSTEM = """당신은 한국 중소형주를 전문으로 하는 PE(사모펀드) 투자심사역이다.
코스닥 시가총액 300억 미만 종목 중 상장폐지 요건 강화(시총·동전주·자본잠식)로
저평가된 기업에서 지분투자 또는 경영권 인수 후보를 발굴하는 것이 목표다.

선정 원칙:
- 시총 대비 자본총계·영업이익·매출이 견실한 기업(저PBR, 흑자 또는 턴어라운드) 우선
- 완전자본잠식 기업은 제외
- 관리종목·거래정지 기업은 장내매수가 불가·제한되므로 '지분투자' 후보에서는 제외.
  단, 자산·현금이 견실하고 대주주의 매각 유인이 큰 경우에는 구주매매·제3자배정
  유상증자 관점의 '인수' 후보로 적극 검토 가능 (상폐 압박 = 협상력 우위)
- 부채비율이 과도한 기업(200% 초과)은 명확한 근거 없이는 제외
- 동전주 요건(주가 1,000원 미만 장기)에 걸린 기업은 액면가·유동성 리스크를 감안
- 사업내용이 구조적으로 사양산업이면 자산가치(청산가치) 관점에서만 고려
- '인수'는 시총이 작아 경영권 확보 비용이 낮고 자산·현금이 풍부한 경우,
  '지분투자'는 사업 자체의 회복·성장 여력이 있는 경우로 구분

정확히 5개를 선정하라. 직전 추천 이력이 주어지면 유지/제외 판단과 그 이유를 thesis에 반영하라."""


def build_universe_text(companies):
    lines = []
    for c in companies:
        fin = c.get("fin", {})
        rev = "/".join(str(fin.get(y, {}).get("rev", "-")) for y in ("2023", "2024", "2025"))
        op = "/".join(str(fin.get(y, {}).get("op", "-")) for y in ("2023", "2024", "2025"))
        flags = []
        if c.get("is_management"):
            flags.append("관리")
        if c.get("trade_stop"):
            flags.append("정지")
        if c.get("equity_impaired"):
            flags.append("잠식")
        if c.get("penny_streak", 0) >= 30:
            flags.append(f"동전주{c['penny_streak']}일")
        lines.append(
            f"{c['code']} {c['name']} | {c.get('sector') or '-'} | {(c.get('biz') or '-')[:70]}"
            f" | 시총{c['mcap']} 주가{c['close']} 부채비율{c.get('debt_ratio', '-')}%"
            f" | 자산{c.get('assets', '-')} 부채{c.get('liab', '-')} 자본{c.get('equity', '-')}"
            f" | 매출{rev} | 영업이익{op}"
            f"{(' | ' + ','.join(flags)) if flags else ''}"
        )
    return "\n".join(lines)


def main():
    if not os.environ.get("OPENAI_API_KEY"):
        print("[오류] OPENAI_API_KEY 미설정", file=sys.stderr)
        return 1

    with open(DATA_PATH, encoding="utf-8") as f:
        data = json.load(f)

    history = []
    if os.path.exists(REC_PATH):
        with open(REC_PATH, encoding="utf-8") as f:
            history = json.load(f)

    prev_note = ""
    if history:
        last = history[-1]
        prev_picks = ", ".join(f"{p['name']}({p['code']}, {p['angle']})" for p in last["picks"])
        prev_note = f"\n\n직전 추천({last['date']}): {prev_picks}"

    user_msg = (
        f"기준일 {data['base_date']} 코스닥 시총 300억 미만 유니버스 "
        f"{len(data['companies'])}종목이다. 금액 단위는 억 원.\n"
        "형식: 코드 기업명 | 업종 | 사업내용 | 시총·주가·부채비율 | 자산·부채·자본 "
        "| 매출(23/24/25) | 영업이익(23/24/25) | 리스크플래그\n\n"
        + build_universe_text(data["companies"])
        + prev_note
    )

    client = OpenAI()
    response = client.chat.completions.create(
        model=MODEL,
        max_completion_tokens=16000,
        messages=[
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user_msg},
        ],
        response_format={
            "type": "json_schema",
            "json_schema": {"name": "picks", "strict": True, "schema": OUTPUT_SCHEMA},
        },
    )

    result = json.loads(response.choices[0].message.content)

    entry = {
        "date": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=9)).strftime("%Y-%m-%d"),
        "base_date": data["base_date"],
        "model": MODEL,
        "market_note": result["market_note"],
        "picks": result["picks"],
    }
    history.append(entry)

    with open(REC_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=1)

    print(f"추천 {len(entry['picks'])}건 저장 (누적 {len(history)}회)")
    for p in entry["picks"]:
        print(f"  [{p['angle']}] {p['name']}({p['code']})")
    print(f"토큰: in={response.usage.prompt_tokens} out={response.usage.completion_tokens}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
