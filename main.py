"""
stock-alert / main.py
─────────────────────
매일 오전 7시 GitHub Actions가 실행하는 메인 스크립트

처리 순서:
1. 전체 종목 중 변동사항(공시/주가급변/뉴스) 발생 종목 감지
2. 변동 종목 전체 + 랜덤 2개 → 상세 리포트 생성
3. 텔레그램으로 전송
"""

import os
import json
import random
import requests
import yfinance as yf
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

# ─── 환경변수 ─────────────────────────────────────────────
DART_API_KEY   = os.environ["DART_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_BASE      = "https://opendart.fss.or.kr/api"
TODAY          = datetime.today().strftime("%Y%m%d")
YESTERDAY      = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
ONE_YEAR_AGO   = (datetime.today() - timedelta(days=365)).strftime("%Y%m%d")


# ════════════════════════════════════════════════════════════
# 텔레그램 전송
# ════════════════════════════════════════════════════════════
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    # 텔레그램 메시지 4096자 제한 → 분할 전송
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    for chunk in chunks:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        })


# ════════════════════════════════════════════════════════════
# DART 기업코드 로드
# ════════════════════════════════════════════════════════════
def load_dart_corps() -> dict:
    import zipfile, io, xml.etree.ElementTree as ET
    cache = "dart_corps.json"
    if os.path.exists(cache):
        mtime = datetime.fromtimestamp(os.path.getmtime(cache))
        if (datetime.now() - mtime).days < 7:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    print("DART 기업코드 다운로드 중...")
    resp = requests.get(f"{DART_BASE}/corpCode.xml?crtfc_key={DART_API_KEY}", timeout=30)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xml_data = z.read(z.namelist()[0])
    root = ET.fromstring(xml_data)
    corps = {}
    for item in root.findall("list"):
        name = item.findtext("corp_name", "").strip()
        code = item.findtext("corp_code", "").strip()
        stock_code = item.findtext("stock_code", "").strip()
        if name and stock_code:
            corps[name] = {"corp_code": code, "stock_code": stock_code}
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(corps, f, ensure_ascii=False, indent=2)
    return corps


# ════════════════════════════════════════════════════════════
# 변동 감지
# ════════════════════════════════════════════════════════════
def check_new_disclosures(corp_code: str) -> list:
    """오늘/어제 새 공시 확인"""
    url = f"{DART_BASE}/list.json"
    params = {
        "crtfc_key": DART_API_KEY,
        "corp_code": corp_code,
        "bgn_de": YESTERDAY,
        "end_de": TODAY,
        "page_count": 10,
    }
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("status") != "000":
            return []
        items = data.get("list", [])
        return [{
            "date":  item.get("rcept_dt", ""),
            "type":  item.get("pblntf_detail_ty_nm", ""),
            "title": item.get("report_nm", ""),
            "url":   f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={item.get('rcept_no','')}",
        } for item in items]
    except:
        return []


def check_price_change(stock_code: str, buy_price: float) -> dict:
    """현재가, 등락률, 수익률 확인"""
    try:
        for suffix in [".KS", ".KQ"]:
            tk = yf.Ticker(f"{stock_code}{suffix}")
            info = tk.info
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if price:
                prev  = info.get("previousClose", price)
                chg   = round((price - prev) / prev * 100, 2) if prev else 0
                ret   = round((price - buy_price) / buy_price * 100, 2) if buy_price else None
                cap   = info.get("marketCap")
                cap_str = f"{cap/1e12:.1f}조" if cap and cap >= 1e12 else (f"{cap/1e8:.0f}억" if cap else "-")
                return {
                    "price": price,
                    "change_pct": chg,
                    "return_pct": ret,
                    "market_cap": cap_str,
                    "52w_high": info.get("fiftyTwoWeekHigh"),
                    "52w_low":  info.get("fiftyTwoWeekLow"),
                    "big_move": abs(chg) >= 5,  # 5% 이상 급변 감지
                }
    except:
        pass
    return {}


def check_news(name: str) -> list:
    """Google News RSS로 최근 뉴스 확인"""
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(name)}+주식&hl=ko&gl=KR&ceid=KR:ko"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.content, "xml")
        items = soup.find_all("item")[:5]
        news = []
        for item in items:
            pub = item.find("pubDate")
            pub_str = pub.text if pub else ""
            # 오늘/어제 뉴스만
            news.append({
                "title":  item.find("title").text if item.find("title") else "",
                "url":    item.find("link").text if item.find("link") else "",
                "date":   pub_str[:16],
            })
        return news
    except:
        return []


# ════════════════════════════════════════════════════════════
# 상세 리포트 생성
# ════════════════════════════════════════════════════════════
def get_analyst_reports(stock_code: str) -> list:
    """네이버금융 애널리스트 리포트"""
    url = f"https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={stock_code}&page=1"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        reports = []
        for row in soup.select("table.type_1 tr"):
            cols = row.find_all("td")
            if len(cols) < 5:
                continue
            title_tag = cols[1].find("a")
            if not title_tag:
                continue
            href = title_tag.get("href", "")
            reports.append({
                "date":   cols[4].get_text(strip=True),
                "firm":   cols[2].get_text(strip=True),
                "title":  title_tag.get_text(strip=True),
                "target": cols[3].get_text(strip=True),
                "url":    f"https://finance.naver.com{href}" if href.startswith("/") else href,
            })
        return reports[:3]
    except:
        return []


def get_financials_summary(corp_code: str) -> str:
    """최근 3년 재무 요약"""
    years = [datetime.today().year - i for i in range(1, 4)]
    lines = []
    for year in years:
        url = f"{DART_BASE}/fnlttSinglAcntAll.json"
        params = {
            "crtfc_key": DART_API_KEY,
            "corp_code":  corp_code,
            "bsns_year":  str(year),
            "reprt_code": "11011",
            "fs_div":     "CFS",
        }
        try:
            data = requests.get(url, params=params, timeout=10).json()
            if data.get("status") != "000":
                continue
            items = data.get("list", [])
            def get_val(acc_id):
                for item in items:
                    if item.get("account_id") == acc_id:
                        v = item.get("thstrm_amount", "").replace(",", "")
                        try:
                            return int(v)
                        except:
                            return None
                return None
            rev = get_val("ifrs-full_Revenue") or get_val("ifrs-full_RevenueFromContractsWithCustomers")
            op  = get_val("ifrs-full_ProfitLossFromOperatingActivities")
            net = get_val("ifrs-full_ProfitLoss")
            def fmt(v):
                if v is None: return "-"
                if abs(v) >= 1e12: return f"{v/1e12:.1f}조"
                if abs(v) >= 1e8:  return f"{v/1e8:.0f}억"
                return str(v)
            lines.append(f"{year}: 매출 {fmt(rev)} | 영업이익 {fmt(op)} | 순이익 {fmt(net)}")
        except:
            continue
    return "\n".join(lines) if lines else "재무 데이터 없음"


# ════════════════════════════════════════════════════════════
# 리포트 포맷
# ════════════════════════════════════════════════════════════
def format_report(name: str, price_data: dict, disclosures: list,
                  news: list, reports: list, financials: str,
                  label: str = "") -> str:

    price     = price_data.get("price", "-")
    chg       = price_data.get("change_pct", 0)
    ret       = price_data.get("return_pct")
    cap       = price_data.get("market_cap", "-")
    high52    = price_data.get("52w_high", "-")
    low52     = price_data.get("52w_low", "-")
    chg_str   = f"+{chg}%" if chg and chg > 0 else f"{chg}%"
    ret_str   = f"+{ret}%" if ret and ret > 0 else (f"{ret}%" if ret else "-")

    lines = [
        f"{'='*35}",
        f"<b>[{label}] {name}</b>",
        f"{'='*35}",
        f"현재가: {price:,}원  ({chg_str})",
        f"시가총액: {cap}",
        f"52주 최고/최저: {high52} / {low52}",
        f"내 수익률: {ret_str}",
        "",
    ]

    # 공시
    if disclosures:
        lines.append("<b>[신규 공시]</b>")
        for d in disclosures[:3]:
            lines.append(f"- {d['date']} {d['title']}")
            lines.append(f"  출처: {d['url']}")
    else:
        lines.append("[신규 공시] 없음")
    lines.append("")

    # 주가 급변
    if price_data.get("big_move"):
        lines.append(f"<b>[주가 급변]</b> 전일 대비 {chg_str} 변동")
        lines.append("")

    # 뉴스
    if news:
        lines.append("<b>[최근 뉴스]</b>")
        for n in news[:3]:
            lines.append(f"- {n['title']}")
            lines.append(f"  {n['url']}")
    lines.append("")

    # 재무 요약
    lines.append("<b>[재무 요약 (최근 3년)]</b>")
    lines.append(financials)
    lines.append("")

    # 애널리스트 리포트
    if reports:
        lines.append("<b>[애널리스트 리포트]</b>")
        for r in reports:
            lines.append(f"- {r['date']} {r['firm']} | {r['title']} | 목표주가: {r['target']}")
            lines.append(f"  {r['url']}")

    return "\n".join(lines)


# ════════════════════════════════════════════════════════════
# 메인
# ════════════════════════════════════════════════════════════
def main():
    print(f"실행 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    with open("stocks.json", encoding="utf-8") as f:
        config = json.load(f)
    stocks = [s for s in config.get("korean", []) if s.get("active")]
    print(f"총 {len(stocks)}개 종목 처리 시작")

    corps = load_dart_corps()

    changed  = []   # 변동 발생 종목
    all_data = []   # 전체 데이터 (랜덤 2개용)

    for stock in stocks:
        name      = stock["name"]
        buy_price = stock.get("buy_price") or 0

        corp_info = corps.get(name)
        if not corp_info:
            clean = name.replace(" ", "")
            corp_info = next((v for k, v in corps.items() if k.replace(" ", "") == clean), None)
        if not corp_info:
            continue

        stock_code = corp_info["stock_code"]
        corp_code  = corp_info["corp_code"]

        # 변동 감지
        disclosures = check_new_disclosures(corp_code)
        price_data  = check_price_change(stock_code, buy_price)
        news        = check_news(name)

        has_change = bool(disclosures) or price_data.get("big_move") or bool(news)

        entry = {
            "name":        name,
            "stock_code":  stock_code,
            "corp_code":   corp_code,
            "buy_price":   buy_price,
            "price_data":  price_data,
            "disclosures": disclosures,
            "news":        news,
        }
        all_data.append(entry)
        if has_change:
            changed.append(entry)

    # 랜덤 2개 선택 (변동 종목 제외)
    non_changed = [s for s in all_data if s not in changed]
    random_picks = random.sample(non_changed, min(2, len(non_changed)))

    # 텔레그램 전송
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    header = f"<b>주식 알림 | {now_str}</b>\n변동 종목: {len(changed)}개 | 랜덤 리포트: {len(random_picks)}개\n"
    send_telegram(header)

    # 변동 종목 리포트
    for entry in changed:
        reports    = get_analyst_reports(entry["stock_code"])
        financials = get_financials_summary(entry["corp_code"])
        msg = format_report(
            entry["name"], entry["price_data"],
            entry["disclosures"], entry["news"],
            reports, financials, label="변동"
        )
        send_telegram(msg)

    # 랜덤 2개 리포트
    for entry in random_picks:
        reports    = get_analyst_reports(entry["stock_code"])
        financials = get_financials_summary(entry["corp_code"])
        msg = format_report(
            entry["name"], entry["price_data"],
            entry["disclosures"], entry["news"],
            reports, financials, label="오늘의 종목"
        )
        send_telegram(msg)

    print("완료")


if __name__ == "__main__":
    main()
