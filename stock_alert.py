"""
台股到價提醒 -> Telegram 通知（持續通知版）

行為：
- 股價未達標：不通知
- 股價達標：每次執行都會發送通知（例如每5分鐘一次），持續到你在 Telegram
  回覆「<股票代號> 謝謝」（例如「2330 謝謝」）給 Bot 為止
- 收到確認訊息後，該股票會被標記為「已確認」，之後就算再次達標也不會再通知，
  除非你手動把 state.json 裡的 "acknowledged" 改回 false（或刪除該股票紀錄）

環境變數（在 GitHub Actions 的 Secrets 設定）：
  BOT_TOKEN      - Telegram Bot Token
  CHAT_ID        - Telegram Chat ID
  STOCK_CODE     - 股票代號，例如 2330
  STOCK_MARKET   - "tse"（上市）或 "otc"（上櫃），預設 tse
  TARGET_PRICE   - 目標價，例如 600
  DIRECTION      - "above"（漲到/超過目標價通知）或 "below"（跌到/低於目標價通知）

狀態檔 state.json 內容範例：
{
  "last_update_id": 123456,
  "acknowledged": false
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
    """取得比 offset 更新的所有訊息"""
    url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={offset + 1}&timeout=0"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("result", [])


def is_ack_message(text: str, stock_code: str) -> bool:
    """判斷訊息是否為「<代號> 謝謝」這類確認回覆，允許中間有空白或標點"""
    if not text:
        return False
    pattern = rf"{re.escape(stock_code)}\s*[，,]?\s*謝謝"
    return re.search(pattern, text) is not None


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_update_id": 0, "acknowledged": False}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def main():
    bot_token = get_env("BOT_TOKEN")
    chat_id = get_env("CHAT_ID")
    stock_code = get_env("STOCK_CODE")
    market = get_env("STOCK_MARKET", default="tse", required=False) or "tse"
    target_price = float(get_env("TARGET_PRICE"))
    direction = get_env("DIRECTION", default="above", required=False) or "above"

    state = load_state()

    # 先檢查有沒有新的確認回覆訊息
    updates = fetch_telegram_updates(bot_token, state.get("last_update_id", 0))
    for update in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), update["update_id"])
        msg = update.get("message", {})
        text = msg.get("text", "")
        if is_ack_message(text, stock_code):
            state["acknowledged"] = True
            print(f"收到確認訊息：「{text}」，之後不再通知（除非重新設定）。")

    if state.get("acknowledged"):
        print("已確認過，略過通知檢查。")
        save_state(state)
        return

    price, name = fetch_price(stock_code, market)
    print(f"目前 {name}({stock_code}) 股價：{price}，目標：{target_price}（{direction}）")

    hit = (direction == "above" and price >= target_price) or (
        direction == "below" and price <= target_price
    )

    if hit:
        arrow = "漲到" if direction == "above" else "跌到"
        msg = (
            f"📈 {name}({stock_code}) 已{arrow} {price} 元\n"
            f"目標價：{target_price}\n\n"
            f"回覆「{stock_code} 謝謝」即可停止此提醒"
        )
        send_telegram(bot_token, chat_id, msg)
        print("已發送 Telegram 通知（尚未確認，下次仍會再通知）。")
    else:
        print("尚未達標，不通知。")

    save_state(state)


if __name__ == "__main__":
    main()
