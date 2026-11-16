"""黑五優惠每日蒐集器（OzBargain RSS → Telegram）

- 只讀公開 RSS，不登入、不繞過反爬機制
- 依店家篩選、只留標價 <= MAX_PRICE 的優惠（沒標價但有 % off 的促銷另列）
- 用 seen.json 去重，只推播新出現的優惠
- 過了 END_DATE 就自動停止
"""
import json
import os
import re
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

HERE = os.path.dirname(os.path.abspath(__file__))
SEEN_PATH = os.path.join(HERE, "seen.json")

SYDNEY = timezone(timedelta(hours=11))  # 11 月雪梨為夏令時間 UTC+11
MAX_PRICE = float(os.environ.get("MAX_PRICE", "1000"))
END_DATE = os.environ.get("END_DATE", "2026-12-01")
TOP_PER_STORE = int(os.environ.get("TOP_PER_STORE", "8"))
BASE = "https://www.ozbargain.com.au"

# 店家名稱 → OzBargain 店家頁的 RSS
FEEDS = {
    "JB Hi-Fi": f"{BASE}/deals/jbhifi.com.au/feed",
    "Big W": f"{BASE}/deals/bigw.com.au/feed",
    "Kmart": f"{BASE}/deals/kmart.com.au/feed",
    "Coles": f"{BASE}/deals/coles.com.au/feed",
    "Woolworths": f"{BASE}/deals/woolworths.com.au/feed",
    "Amazon AU": f"{BASE}/deals/amazon.com.au/feed",
    "Officeworks": f"{BASE}/deals/officeworks.com.au/feed",
    "Harvey Norman": f"{BASE}/deals/harveynorman.com.au/feed",
}

PRICE_RE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?")
PERCENT_RE = re.compile(r"\d{1,2}\s?%\s?off", re.I)


def first_price(title):
    """回傳標題中第一個價格（float），沒有就回傳 None。"""
    m = PRICE_RE.search(title)
    if not m:
        return None
    whole = m.group(1).replace(",", "")
    cents = m.group(2) or "0"
    return float(f"{whole}.{cents}")


def parse_feed(xml_text, store):
    """把 RSS 文字解析成優惠清單。"""
    root = ET.fromstring(xml_text)
    deals = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        guid = (item.findtext("guid") or link).strip()
        votes = 0
        expired = False
        for el in item:
            if el.tag.endswith("}meta"):
                try:
                    votes = int(el.attrib.get("votes-pos", "0")) - int(el.attrib.get("votes-neg", "0"))
                except ValueError:
                    votes = 0
                expired = el.attrib.get("expiry") is not None and "expired" in el.attrib.get("expiry", "")
        if not title or expired:
            continue
        pub = item.findtext("pubDate")
        try:
            published = parsedate_to_datetime(pub).astimezone(SYDNEY).date().isoformat() if pub else ""
        except (TypeError, ValueError):
            published = ""
        deals.append({
            "store": store,
            "title": title,
            "link": link,
            "guid": guid,
            "price": first_price(title),
            "votes": votes,
            "published": published,
        })
    return deals


def keep(deal, max_price=MAX_PRICE):
    """標價 <= 上限，或沒標價但是 % off 的促銷，才保留。"""
    if deal["price"] is not None:
        return deal["price"] <= max_price
    return bool(PERCENT_RE.search(deal["title"]))


def load_seen():
    try:
        with open(SEEN_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_seen(seen, today):
    cutoff = (datetime.fromisoformat(today) - timedelta(days=45)).date().isoformat()
    seen = {k: v for k, v in seen.items() if v >= cutoff}
    with open(SEEN_PATH, "w", encoding="utf-8") as f:
        json.dump(seen, f, ensure_ascii=False, indent=0, sort_keys=True)


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "personal-bf-deal-digest/1.0 (RSS reader)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def build_messages(new_deals, failed, today, limit=3800):
    """依店家分組、按票數排序，切成 Telegram 能接受的長度。"""
    lines = [f"🛒 黑五新優惠 {today}（≤${MAX_PRICE:,.0f}）", "來源：OzBargain 社群（C級線索，買前看官方頁確認）", ""]
    by_store = {}
    for d in new_deals:
        by_store.setdefault(d["store"], []).append(d)
    if not by_store:
        lines.append("今天沒有新的優惠。")
    for store in FEEDS:
        items = sorted(by_store.get(store, []), key=lambda d: d["votes"], reverse=True)
        if not items:
            continue
        lines.append(f"【{store}】{len(items)} 則")
        for d in items[:TOP_PER_STORE]:
            price = f"${d['price']:,.2f}" if d["price"] is not None else "％"
            lines.append(f"• {price}｜👍{d['votes']}｜{d['title'][:90]}\n  {d['link']}")
        if len(items) > TOP_PER_STORE:
            lines.append(f"  …另有 {len(items) - TOP_PER_STORE} 則")
        lines.append("")
    if failed:
        lines.append("⚠️ 讀取失敗：" + "、".join(failed))
    msgs, cur = [], ""
    for line in lines:
        if len(cur) + len(line) + 1 > limit:
            msgs.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        msgs.append(cur)
    return msgs


def send_telegram(text):
    token, chat = os.environ.get("BOT_TOKEN"), os.environ.get("CHAT_ID")
    if not token or not chat:
        print(text)
        return
    data = json.dumps({"chat_id": chat, "text": text, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def main():
    today = datetime.now(SYDNEY).date().isoformat()
    if today > END_DATE:
        print(f"已過 {END_DATE}，蒐集器停止。")
        return 0
    seen = load_seen()
    new_deals, failed = [], []
    for store, url in FEEDS.items():
        try:
            deals = parse_feed(fetch(url), store)
        except Exception as e:  # 單一來源失敗不影響其他店家
            print(f"{store} 失敗：{e}", file=sys.stderr)
            failed.append(store)
            continue
        for d in deals:
            if d["guid"] in seen or not keep(d):
                continue
            seen[d["guid"]] = today
            new_deals.append(d)
        time.sleep(2)  # 對來源網站客氣一點
    for msg in build_messages(new_deals, failed, today):
        send_telegram(msg)
    save_seen(seen, today)
    print(f"新優惠 {len(new_deals)} 則，失敗 {len(failed)} 個來源")
    return 0


if __name__ == "__main__":
    sys.exit(main())
