"""장바구니 날씨 생성기 — 대장부 앱이 읽는 basket.json을 만든다.

GitHub Actions(.github/workflows/basket.yml)가 매주 목요일 아침에 실행한다.
표준 라이브러리만 사용. 키는 환경 변수(GitHub Secrets)로만 받는다.

  KAMIS_KEY, KAMIS_ID   aT KAMIS Open-API (농축수산물 소매가)
  OPINET_KEY            한국석유공사 오피넷 (휘발유 전국 평균)
  ECOS_KEY              한국은행 ECOS (원/달러 환율)

규칙 (대장부 설계, 2026-11-13)
- 예측 금지: "올랐어요/내렸어요"만. 구매 권유 문구 금지.
- 품목: 지난주 대비 ±10% 이상이고 한 달 전 대비 같은 방향 → 내린 것 1개(sunny), 오른 것 1개(cloudy).
  지난주 기록이 없으면(첫 실행) 한 달 전 대비 ±15% 이상으로 대신 판단.
- 휘발유: 지난주 대비 1.5% 이상이면 후보.
- 환율: 한 달 새 20원 이상 움직이면 한마디(tip).
- 소스 하나가 실패해도 나머지로 만든다. 전부 실패하면 기존 파일을 건드리지 않는다.

로컬 확인:  python3 scripts/basket.py --dry-run   (키 없으면 해당 소스는 건너뜀)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / 'basket.json'
HISTORY = ROOT / 'basket_history.json'
KST = dt.timezone(dt.timedelta(hours=9))

# 장바구니 품목: (KAMIS 품목명에 포함될 키워드, 앱에 보일 이름)
WATCH = [
    ('배추', '배추'), ('무', '무'), ('양파', '양파'), ('대파', '대파'), ('감자', '감자'),
    ('오이', '오이'), ('애호박', '애호박'), ('상추', '상추'), ('시금치', '시금치'),
    ('쌀', '쌀'), ('사과', '사과'), ('배', '배'), ('달걀', '계란'), ('계란', '계란'),
    ('돼지', '삼겹살'), ('닭', '닭고기'), ('고등어', '고등어'),
]
KAMIS_CATEGORIES = ['100', '200', '400', '500', '600']  # 식량, 채소, 과일, 축산, 수산


def get(url: str, timeout: int = 20) -> str:
    req = urllib.request.Request(url, headers={'User-Agent': 'daejangbu-basket/1.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8')


def num(s) -> float | None:
    if s is None:
        return None
    s = str(s).replace(',', '').strip()
    try:
        v = float(s)
        return v if v > 0 else None
    except ValueError:
        return None


# ── KAMIS ──────────────────────────────────────────────────────────

def kamis_items(today: dt.date) -> list[dict]:
    """소매가 일별: [{name, unit, price, month_ago}] (품목당 1개, 대표 품종)."""
    key, cid = os.environ.get('KAMIS_KEY'), os.environ.get('KAMIS_ID')
    if not key or not cid:
        print('KAMIS: 키 없음 — 건너뜀')
        return []
    found: dict[str, dict] = {}
    for cat in KAMIS_CATEGORIES:
        q = urllib.parse.urlencode({
            'action': 'dailyPriceByCategoryList', 'p_product_cls_code': '01',
            'p_regday': today.isoformat(), 'p_convert_kg_yn': 'N',
            'p_item_category_code': cat, 'p_cert_key': key, 'p_cert_id': cid,
            'p_returntype': 'json',
        })
        try:
            data = json.loads(get(f'https://www.kamis.or.kr/service/price/xml.do?{q}'))
        except Exception as e:  # noqa: BLE001
            print(f'KAMIS {cat}: 실패 {e}')
            continue
        rows = (data.get('data') or {}).get('item') or []
        if isinstance(rows, dict):
            rows = [rows]
        for row in rows:
            name = f"{row.get('item_name', '')}"
            label = next((show for kw, show in WATCH if kw in name), None)
            if not label or label in found:
                continue
            # 응답의 dayN 라벨로 "당일"과 "1개월전" 열을 찾는다 (열 순서 변화 대비)
            cols = {str(row.get(f'day{i}', '')): row.get(f'dpr{i}') for i in range(1, 8)}
            price = next((num(v) for k, v in cols.items() if '당일' in k), num(row.get('dpr1')))
            month = next((num(v) for k, v in cols.items() if '1개월' in k), None)
            if price is None:
                continue
            found[label] = {
                'name': label, 'unit': str(row.get('unit', '')).strip() or None,
                'price': price, 'month_ago': month, 'source': 'KAMIS',
            }
    print(f'KAMIS: {len(found)}개 품목')
    return list(found.values())


# ── 오피넷 ─────────────────────────────────────────────────────────

def opinet_gasoline() -> dict | None:
    """최근 7일 휘발유 전국 평균 → {price, week_ago}."""
    key = os.environ.get('OPINET_KEY')
    if not key:
        print('오피넷: 키 없음 — 건너뜀')
        return None
    try:
        data = json.loads(get(
            f'https://www.opinet.co.kr/api/avgRecentPrice.do?out=json&code={key}&prodcd=B027'))
        oil = sorted(data['RESULT']['OIL'], key=lambda r: r['DATE'])
        if len(oil) < 2:
            return None
        return {'name': '휘발유', 'unit': '1L', 'price': num(oil[-1]['PRICE']),
                'week_ago': num(oil[0]['PRICE']), 'source': '오피넷'}
    except Exception as e:  # noqa: BLE001
        print(f'오피넷: 실패 {e}')
        return None


# ── 한국은행 ECOS ──────────────────────────────────────────────────

def ecos_usd_change(today: dt.date) -> float | None:
    """원/달러 매매기준율: 최근값 − 약 한 달 전 값."""
    key = os.environ.get('ECOS_KEY')
    if not key:
        print('ECOS: 키 없음 — 건너뜀')
        return None

    def window(end: dt.date) -> list[float]:
        start = end - dt.timedelta(days=6)
        url = (f'https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/1/10/731Y001/D/'
               f'{start:%Y%m%d}/{end:%Y%m%d}/0000001')
        rows = json.loads(get(url)).get('StatisticSearch', {}).get('row', [])
        return [v for v in (num(r['DATA_VALUE']) for r in rows) if v]

    try:
        now = window(today)
        then = window(today - dt.timedelta(days=30))
        if not now or not then:
            return None
        return now[-1] - then[-1]
    except Exception as e:  # noqa: BLE001
        print(f'ECOS: 실패 {e}')
        return None


# ── 선별 ──────────────────────────────────────────────────────────

def pct(a: float, b: float) -> float:
    return (a - b) / b * 100


def pick(items: list[dict], last_week: dict[str, float], fuel: dict | None):
    """(sunny, cloudy) 후보 — 각 1개."""
    cands = []
    for it in items:
        prev = last_week.get(it['name'])
        month = it.get('month_ago')
        if prev:
            w = pct(it['price'], prev)
            same_dir = month is None or (pct(it['price'], month) * w > 0)
            if abs(w) >= 10 and same_dir:
                cands.append((w, it, '지난주'))
        elif month:
            m = pct(it['price'], month)
            if abs(m) >= 15:
                cands.append((m, it, '한 달 전'))
    if fuel and fuel.get('price') and fuel.get('week_ago'):
        w = pct(fuel['price'], fuel['week_ago'])
        if abs(w) >= 1.5:
            cands.append((w, fuel, '지난주'))

    def as_json(c):
        change, it, compare = c
        return {'name': it['name'], 'change_pct': round(change), 'compare': compare,
                'price': round(it['price']), 'unit': it.get('unit'), 'source': it['source']}

    downs = sorted((c for c in cands if c[0] < 0), key=lambda c: c[0])
    ups = sorted((c for c in cands if c[0] > 0), key=lambda c: -c[0])
    return ([as_json(downs[0])] if downs else [], [as_json(ups[0])] if ups else [])


def main() -> int:
    dry = '--dry-run' in sys.argv
    today = dt.datetime.now(KST).date()

    history = json.loads(HISTORY.read_text()) if HISTORY.exists() else {}
    # 6~8일 전 기록 중 가장 가까운 것을 "지난주"로
    last_week: dict[str, float] = {}
    for back in (7, 6, 8):
        snap = history.get((today - dt.timedelta(days=back)).isoformat())
        if snap:
            last_week = snap
            break

    items = kamis_items(today)
    fuel = opinet_gasoline()
    usd = ecos_usd_change(today)

    if not items and not fuel and usd is None:
        print('모든 소스 실패 — basket.json 유지')
        return 0

    sunny, cloudy = pick(items, last_week, fuel)
    tip = None
    if usd is not None and abs(usd) >= 20:
        tip = ({'text': f'환율이 한 달 새 {abs(round(usd))}원 올랐어요. 해외직구는 잠깐 미뤄도 돼요.',
                'source': '한국은행'} if usd > 0 else
               {'text': f'환율이 한 달 새 {abs(round(usd))}원 내렸어요. 해외직구 부담이 조금 줄었어요.',
                'source': '한국은행'})

    out = {'schema': 1, 'base_date': today.isoformat(),
           'updated_at': dt.datetime.now(KST).isoformat(timespec='seconds'),
           'sunny': sunny, 'cloudy': cloudy}
    if tip:
        out['tip'] = tip

    # 이번 주 가격 기록 (최근 60일만 보관)
    snap = {it['name']: it['price'] for it in items}
    if fuel and fuel.get('price'):
        snap['휘발유'] = fuel['price']
    history[today.isoformat()] = snap
    cutoff = (today - dt.timedelta(days=60)).isoformat()
    history = {k: v for k, v in history.items() if k >= cutoff}

    print(json.dumps(out, ensure_ascii=False, indent=1))
    if dry:
        return 0
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1) + '\n')
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, indent=1) + '\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
