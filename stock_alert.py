"""
多股票雙向到價提醒 -> Telegram 通知（股票清單改由 Google 試算表提供）

股票清單來源：一份「發布到網路」的 Google 試算表（CSV 格式連結）。
不懂程式的人只要打開這份試算表、像填 Excel 一樣輸入資料即可，
不需要碰任何程式碼或 GitHub。

試算表欄位順序（固定 4 欄，順序不能亂，第一列是標題列會被自動跳過）：
  A欄：股票代號       例如 00712
  B欄：市場           tse（上市）或 otc（上櫃），留空預設 tse
  C欄：漲到目標價      例如 9，不需要這個方向就留空
  D欄：跌到目標價      例如 7.9，不需要這個方向就留空

環境變數（GitHub Actions Secrets）：
  BOT_TOKEN       - Telegram Bot Token
  CHAT_ID         - Telegram Chat ID
  STOCKS_CSV_URL  - Google 試算表發布後的 CSV 連結

行為：
- 每支股票的「漲到」「跌到」各自獨立追蹤達標與確認狀態
- 達標後每次執行都會持續通知，直到你回覆「<該股票代號> 謝謝」
- 確認後如果價格回到目標區間內，該方向會自動重置，之後再次達標會重新通知
"""

import os
import json
import re
import sys
import csv
import io
import urllib.request

STATE_FILE = "state.json"


def get_env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"缺少環境變數：{name}")
        sys.exit(1)
    return val


def load_stocks(csv_url: str) -> list:
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8-sig")  # utf-8-sig 處理 Google 試算表常見的 BOM

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return []

    stocks = []
    for row in rows[1:]:  # 第一列是標題，跳過
        if not row or not row[0].strip():
            continue
        code = row[0].strip()
        market = (row[1].strip().lower() if len(row) > 1 and row[1].strip() else "tse")
        above_raw = row[2].strip() if len(row) > 2 else ""
        below_raw = row[3].strip() if len(row) > 3 else ""

        try:
            above = float(above_raw) if above_raw else None
        except ValueError:
            print(f"{code}：漲到目標價「{above_raw}」不是有效數字，略過這個方向。")
            above = None

        try:
            below = float(below_raw) if below_raw else None
        except ValueError:
            print(f"{code}：跌到目標價「{below_raw}」不是有效數字，略過這個方向。")
            below = None

        stocks.append({"code": code, "market": market, "above": above, "below": below})

    return stocks


def fetch_price(stock_code: str, market: str):
    prefix = "tse" if market == "tse" else "otc"
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch={prefix}_{stock_code}.tw"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://mis.twse.com.tw/stock/index.jsp",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    arr = data.get("msgArray")
    if not arr:
        raise RuntimeError(f"查無股票資料，回傳內容：{data}")

    info = arr[0]
    price_str = info.get("z")
    if price_str in (None, "-", ""):
        price_str = info.get("b", "").split("_")[0] or info.get("y")
    if not price_str or price_str == "-":
        raise RuntimeError(f"目前查無有效成交價，原始資料：{info}")

    return float(price_str), info.get("n", stock_code)


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def fetch_telegram_updates(bot_token: str, offset: int):
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={offset + 1}&timeout=0"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", [])


def is_ack_message(text: str, stock_code: str) -> bool:
    if not text:
        return False
    pattern = rf"{re.escape(stock_code)}\s*[，,]?\s*謝謝"
    return re.search(pattern, text) is not None


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_update_id": 0, "stocks": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    bot_token = get_env("BOT_TOKEN")
    chat_id = get_env("CHAT_ID")
    csv_url = get_env("STOCKS_CSV_URL")

    stocks = load_stocks(csv_url)
    if not stocks:
        print("試算表目前沒有任何股票資料，結束。")
        return

    state = load_state()
    state.setdefault("stocks", {})

    updates = fetch_telegram_updates(bot_token, state.get("last_update_id", 0))
    for update in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), update["update_id"])
        text = update.get("message", {}).get("text", "")
        for s in stocks:
            if is_ack_message(text, s["code"]):
                st = state["stocks"].setdefault(
                    s["code"], {"acknowledged_above": False, "acknowledged_below": False}
                )
                st["acknowledged_above"] = True
                st["acknowledged_below"] = True
                print(f"收到 {s['code']} 的確認訊息，該股票兩方向皆已標記確認。")

    for s in stocks:
        code = s["code"]
        market = s["market"]
        target_above = s["above"]
        target_below = s["below"]

        if target_above is None and target_below is None:
            print(f"{code} 沒有設定任何目標價，略過。")
            continue

        st = state["stocks"].setdefault(
            code, {"acknowledged_above": False, "acknowledged_below": False}
        )

        try:
            price, name = fetch_price(code, market)
        except Exception as e:
            print(f"{code} 抓取股價失敗：{e}")
            continue

        print(f"目前 {name}({code}) 股價：{price}"
              f"{'，漲到目標：' + str(target_above) if target_above is not None else ''}"
              f"{'，跌到目標：' + str(target_below) if target_below is not None else ''}")

        hit_above = target_above is not None and price >= target_above
        hit_below = target_below is not None and price <= target_below

        if target_above is not None and price < target_above:
            st["acknowledged_above"] = False
        if target_below is not None and price > target_below:
            st["acknowledged_below"] = False

        if hit_above and not st["acknowledged_above"]:
            msg = (
                f"📈 {name}({code}) 已漲到 {price} 元\n"
                f"目標價（漲）：{target_above}\n\n"
                f"回覆「{code} 謝謝」即可停止此提醒"
            )
            send_telegram(bot_token, chat_id, msg)
            print(f"{code}：已發送「漲到」通知。")

        if hit_below and not st["acknowledged_below"]:
            msg = (
                f"📉 {name}({code}) 已跌到 {price} 元\n"
                f"目標價（跌）：{target_below}\n\n"
                f"回覆「{code} 謝謝」即可停止此提醒"
            )
            send_telegram(bot_token, chat_id, msg)
            print(f"{code}：已發送「跌到」通知。")

    save_state(state)


if __name__ == "__main__":
    main()
