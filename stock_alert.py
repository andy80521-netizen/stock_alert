"""
台股雙向到價提醒 -> Telegram 通知

行為：
- 股價漲到 TARGET_PRICE_ABOVE 以上 -> 通知（持續通知，直到確認）
- 股價跌到 TARGET_PRICE_BELOW 以下 -> 通知（持續通知，直到確認）
- 兩個方向各自獨立追蹤，其中一個確認了不影響另一個
- 在 Telegram 回覆「<股票代號> 謝謝」（例如「00712 謝謝」）即可把「目前正在響的那個(些)方向」都標記為已確認，停止通知
  之後如果價格從已確認的區間又反向穿越回來，會重新開始通知（見下方邏輯說明）

環境變數（GitHub Actions Secrets）：
  BOT_TOKEN            - Telegram Bot Token
  CHAT_ID              - Telegram Chat ID
  STOCK_CODE           - 股票代號，例如 00712
  STOCK_MARKET         - "tse"（上市）或 "otc"（上櫃），預設 tse
  TARGET_PRICE_ABOVE   - 漲到這個價位通知，例如 9（留空或不設定 = 不監控這個方向）
  TARGET_PRICE_BELOW   - 跌到這個價位通知，例如 7.9（留空或不設定 = 不監控這個方向）

state.json 範例：
{
  "last_update_id": 123456,
  "acknowledged_above": false,
  "acknowledged_below": false
}
"""

import os
import json
import re
import sys
import urllib.request

STATE_FILE = "state.json"


def get_env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"缺少環境變數：{name}")
        sys.exit(1)
    return val


def get_optional_float(name):
    val = os.environ.get(name, "")
    val = val.strip() if val else ""
    return float(val) if val else None


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
    return {"last_update_id": 0, "acknowledged_above": False, "acknowledged_below": False}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    bot_token = get_env("BOT_TOKEN")
    chat_id = get_env("CHAT_ID")
    stock_code = get_env("STOCK_CODE")
    market = get_env("STOCK_MARKET", default="tse", required=False) or "tse"
    target_above = get_optional_float("TARGET_PRICE_ABOVE")
    target_below = get_optional_float("TARGET_PRICE_BELOW")

    if target_above is None and target_below is None:
        print("TARGET_PRICE_ABOVE 和 TARGET_PRICE_BELOW 至少要設定一個。")
        sys.exit(1)

    state = load_state()
    state.setdefault("acknowledged_above", False)
    state.setdefault("acknowledged_below", False)

    # 檢查有沒有新的確認回覆訊息
    updates = fetch_telegram_updates(bot_token, state.get("last_update_id", 0))
    acked_now = False
    for update in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), update["update_id"])
        msg = update.get("message", {})
        text = msg.get("text", "")
        if is_ack_message(text, stock_code):
            state["acknowledged_above"] = True
            state["acknowledged_below"] = True
            acked_now = True
    if acked_now:
        print("收到確認訊息，兩個方向的通知都已標記為確認。")

    price, name = fetch_price(stock_code, market)
    print(f"目前 {name}({stock_code}) 股價：{price}")
    if target_above is not None:
        print(f"  漲到目標：{target_above}")
    if target_below is not None:
        print(f"  跌到目標：{target_below}")

    hit_above = target_above is not None and price >= target_above
    hit_below = target_below is not None and price <= target_below

    # 價格若已經回到目標區間之外，重置該方向的確認狀態，讓下次再次達標時能重新通知
    if target_above is not None and price < target_above:
        state["acknowledged_above"] = False
    if target_below is not None and price > target_below:
        state["acknowledged_below"] = False

    notified = False

    if hit_above and not state["acknowledged_above"]:
        msg = (
            f"📈 {name}({stock_code}) 已漲到 {price} 元\n"
            f"目標價（漲）：{target_above}\n\n"
            f"回覆「{stock_code} 謝謝」即可停止此提醒"
        )
        send_telegram(bot_token, chat_id, msg)
        notified = True
        print("已發送「漲到」通知。")

    if hit_below and not state["acknowledged_below"]:
        msg = (
            f"📉 {name}({stock_code}) 已跌到 {price} 元\n"
            f"目標價（跌）：{target_below}\n\n"
            f"回覆「{stock_code} 謝謝」即可停止此提醒"
        )
        send_telegram(bot_token, chat_id, msg)
        notified = True
        print("已發送「跌到」通知。")

    if not notified:
        print("尚未達標，或已確認過，不通知。")

    save_state(state)


if __name__ == "__main__":
    main()
