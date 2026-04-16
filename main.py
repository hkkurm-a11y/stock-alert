"""
stock-alert / main.py
수정 사항:
1. 리포트 링크 클릭 가능하게 (HTML a 태그)
2. 목표주가 파싱 수정
3. 신규 리포트 있으면 최근 리포트 섹션 생략
"""

import os, json, time, random, requests, yfinance as yf
from datetime import datetime, timedelta
from bs4 import BeautifulSoup

DART_API_KEY     = os.environ["DART_API_KEY"]
TELEGRAM_TOKEN   = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
DART_BASE        = "https://opendart.fss.or.kr/api"
TODAY            = datetime.today().strftime("%Y%m%d")
YESTERDAY        = (datetime.today() - timedelta(days=1)).strftime("%Y%m%d")
TODAY_S          = datetime.today().strftime("%y.%m.%d")
YESTERDAY_S      = (datetime.today() - timedelta(days=1)).strftime("%y.%m.%d")


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        })
        time.sleep(0.3)


def link(title, url):
    """클릭 가능한 텔레그램 링크"""
    t = title.replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
    return f'<a href="{url}">{t}</a>'


def load_dart_corps():
    import zipfile, io, xml.etree.ElementTree as ET
    cache = "dart_corps.json"
    if os.path.exists(cache):
        if (datetime.now() - datetime.fromtimestamp(os.path.getmtime(cache))).days < 7:
            with open(cache, encoding="utf-8") as f:
                return json.load(f)
    print("DART 기업코드 다운로드 중...")
    resp = requests.get(f"{DART_BASE}/corpCode.xml?crtfc_key={DART_API_KEY}", timeout=30)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as z:
        xml_data = z.read(z.namelist()[0])
    root  = ET.fromstring(xml_data)
    corps = {}
    for item in root.findall("list"):
        name, code, sc = (item.findtext(k,"").strip() for k in ["corp_name","corp_code","stock_code"])
        if name and sc:
            corps[name] = {"corp_code": code, "stock_code": sc}
    with open(cache, "w", encoding="utf-8") as f:
        json.dump(corps, f, ensure_ascii=False, indent=2)
    return corps


def check_new_disclosures(corp_code):
    try:
        resp = requests.get(f"{DART_BASE}/list.json", params={
            "crtfc_key": DART_API_KEY, "corp_code": corp_code,
            "bgn_de": YESTERDAY, "end_de": TODAY, "page_count": 10}, timeout=10)
        data = resp.json()
        if data.get("status") != "000": return []
        return [{"date": i.get("rcept_dt",""), "type": i.get("pblntf_detail_ty_nm",""),
                 "title": i.get("report_nm",""),
                 "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={i.get('rcept_no','')}"}
                for i in data.get("list", [])]
    except: return []


def get_reports(stock_code, new_only=False):
    """네이버금융 리포트 — 목표주가 파싱 수정"""
    url = f"https://finance.naver.com/research/company_list.naver?searchType=itemCode&itemCode={stock_code}&page=1"
    try:
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        reports = []
        for row in soup.select("table.type_1 tr"):
            cols = row.find_all("td")
            if len(cols) < 5: continue
            ta = cols[1].find("a")
            if not ta: continue
            date = cols[4].get_text(strip=True)
            if new_only and date not in [TODAY_S, YESTERDAY_S]: continue

            href = ta.get("href", "")
            full_url = f"https://finance.naver.com{href}" if href.startswith("/") else href

            # 목표주가: cols[3] 안의 숫자 추출
            target_raw = cols[3].get_text(strip=True)
            target = target_raw if target_raw and target_raw != "-" else "미제시"

            reports.append({
                "date":   date,
                "firm":   cols[2].get_text(strip=True),
                "title":  ta.get_text(strip=True),
                "target": target,
                "url":    full_url,
            })
        return reports[:3]
    except: return []


def get_price(stock_code, buy_price):
    try:
        for sfx in [".KS", ".KQ"]:
            info  = yf.Ticker(f"{stock_code}{sfx}").info
            price = info.get("regularMarketPrice") or info.get("currentPrice")
            if not price: continue
            prev  = info.get("previousClose", price)
            chg   = round((price - prev) / prev * 100, 2) if prev else 0
            ret   = round((price - buy_price) / buy_price * 100, 2) if buy_price else None
            cap   = info.get("marketCap")
            return {
                "price":      price,
                "change_pct": chg,
                "return_pct": ret,
                "market_cap": f"{cap/1e12:.1f}조" if cap and cap >= 1e12 else (f"{cap/1e8:.0f}억" if cap else "-"),
                "52w_high":   info.get("fiftyTwoWeekHigh"),
                "52w_low":    info.get("fiftyTwoWeekLow"),
            }
    except: pass
    return {}


def get_news(name):
    try:
        soup = BeautifulSoup(requests.get(
            f"https://news.google.com/rss/search?q={requests.utils.quote(name)}+주식&hl=ko&gl=KR&ceid=KR:ko",
            headers={"User-Agent": "Mozilla/5.0"}, timeout=10).content, "xml")
        return [{"title": i.find("title").text if i.find("title") else "",
                 "url":   i.find("link").text  if i.find("link")  else ""}
                for i in soup.find_all("item")[:3]]
    except: return []


def get_financials(corp_code):
    lines = []
    for year in [datetime.today().year - i for i in range(1, 4)]:
        try:
            data = requests.get(f"{DART_BASE}/fnlttSinglAcntAll.json", params={
                "crtfc_key": DART_API_KEY, "corp_code": corp_code,
                "bsns_year": str(year), "reprt_code": "11011", "fs_div": "CFS"}, timeout=10).json()
            if data.get("status") != "000": continue
            items = data.get("list", [])

            def gv(aid):
                for i in items:
                    if i.get("account_id") == aid:
                        try: return int(i.get("thstrm_amount","").replace(",",""))
                        except: return None
                return None

            def fmt(v):
                if v is None:        return "-"
                if abs(v) >= 1e12:   return f"{v/1e12:.1f}조"
                if abs(v) >= 1e8:    return f"{v/1e8:.0f}억"
                return str(v)

            rev = gv("ifrs-full_Revenue") or gv("ifrs-full_RevenueFromContractsWithCustomers")
            op  = gv("ifrs-full_ProfitLossFromOperatingActivities") or gv("dart_OperatingIncomeLoss")
            net = gv("ifrs-full_ProfitLoss")
            lines.append(f"{year}: 매출 {fmt(rev)} | 영업이익 {fmt(op)} | 순이익 {fmt(net)}")
            time.sleep(0.3)
        except: continue
    return "\n".join(lines) if lines else "재무 데이터 없음"


def format_report(name, price_data, disclosures, new_reports, all_reports, news, financials, label):
    p   = price_data.get("price", "-")
    chg = price_data.get("change_pct", 0)
    ret = price_data.get("return_pct")
    cs  = f"+{chg}%" if chg and chg > 0 else f"{chg}%"
    rs  = f"+{ret}%" if ret and ret > 0 else (f"{ret}%" if ret else "-")

    lines = [
        f"<b>[{label}] {name}</b>",
        f"현재가: {p:,}원 ({cs})  |  시총: {price_data.get('market_cap','-')}",
        f"52주: {price_data.get('52w_high','-')} / {price_data.get('52w_low','-')}  |  내 수익률: {rs}",
        "",
    ]

    # 공시
    if disclosures:
        lines.append("<b>[신규 공시]</b>")
        for d in disclosures[:3]:
            lines.append(f"- {d['date']}  {link(d['title'], d['url'])}")
    else:
        lines.append("[신규 공시] 없음")
    lines.append("")

    # 신규 리포트 (있으면 표시, 없으면 생략)
    if new_reports:
        lines.append("<b>[신규 리포트]</b>")
        for r in new_reports:
            lines.append(f"- {r['date']}  {r['firm']}  |  목표주가: {r['target']}")
            lines.append(f"  {link(r['title'], r['url'])}")
        lines.append("")

    # 재무 요약
    lines.append("<b>[재무 요약 (최근 3년)]</b>")
    lines.append(financials)
    lines.append("")

    # 최근 리포트 — 신규 리포트가 없을 때만 표시
    if all_reports and not new_reports:
        lines.append("<b>[최근 리포트]</b>")
        for r in all_reports[:3]:
            lines.append(f"- {r['date']}  {r['firm']}  |  목표주가: {r['target']}")
            lines.append(f"  {link(r['title'], r['url'])}")
        lines.append("")

    # 뉴스
    if news:
        lines.append("<b>[최근 뉴스]</b>")
        for n in news:
            if n['title'] and n['url']:
                lines.append(f"- {link(n['title'], n['url'])}")

    return "\n".join(lines)


def main():
    print(f"실행 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    with open("stocks.json", encoding="utf-8") as f:
        stocks = [s for s in json.load(f).get("korean", []) if s.get("active")]
    print(f"총 {len(stocks)}개 종목")

    corps    = load_dart_corps()
    changed  = []
    all_data = []

    for stock in stocks:
        name      = stock["name"]
        buy_price = stock.get("buy_price") or 0
        ci        = corps.get(name) or next(
            (v for k, v in corps.items() if k.replace(" ","") == name.replace(" ","")), None)
        if not ci: continue

        disclosures = check_new_disclosures(ci["corp_code"])
        new_reports = get_reports(ci["stock_code"], new_only=True)
        price_data  = get_price(ci["stock_code"], buy_price)
        has_change  = bool(disclosures) or bool(new_reports)

        entry = {
            "name":        name,
            "stock_code":  ci["stock_code"],
            "corp_code":   ci["corp_code"],
            "buy_price":   buy_price,
            "price_data":  price_data,
            "disclosures": disclosures,
            "new_reports": new_reports,
        }
        all_data.append(entry)
        if has_change:
            changed.append(entry)
        time.sleep(0.5)

    print(f"변동 종목: {len(changed)}개")
    non_changed  = [s for s in all_data if s not in changed]
    random_picks = random.sample(non_changed, min(2, len(non_changed)))

    send_telegram(
        f"<b>주식 알림 | {datetime.now().strftime('%Y-%m-%d %H:%M')}</b>\n"
        f"변동 종목: {len(changed)}개 (공시/신규리포트)  |  랜덤 리포트: {len(random_picks)}개"
    )

    for entry in changed + random_picks:
        label       = "변동" if entry in changed else "오늘의 종목"
        all_reports = get_reports(entry["stock_code"])
        financials  = get_financials(entry["corp_code"])
        news        = get_news(entry["name"])
        msg = format_report(
            entry["name"], entry["price_data"],
            entry["disclosures"], entry["new_reports"],
            all_reports, news, financials, label
        )
        send_telegram(msg)
        time.sleep(1)

    print("완료")


if __name__ == "__main__":
    main()
