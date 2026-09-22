"""
多股票雙向到價提醒 + Telegram指令式歷史數據分析

【到價提醒部分】股票清單來自 Google 試算表發布的 CSV（見 STOCKS_CSV_URL）
【隨選分析部分】在 Telegram 傳「分析 <股票代號>」給 Bot，例如「分析 00712」或「分析 AAPL」，
                下次排程執行時會抓歷史資料跑分析，並把結果回傳到 Telegram。

⚠️ 分析結果僅為歷史統計，不構成投資建議，不保證未來會重演。

環境變數（GitHub Actions Secrets）：
  BOT_TOKEN       - Telegram Bot Token
  CHAT_ID         - Telegram Chat ID
  STOCKS_CSV_URL  - Google 試算表發布的 CSV 連結
"""

import os
import json
import re
import sys
import csv
import io
import urllib.request
import numpy as np

try:
    import yfinance as yf
except ImportError:
    yf = None

STATE_FILE = "state.json"


# ========== 共用工具 ==========

def get_env(name, default=None, required=True):
    val = os.environ.get(name, default)
    if required and not val:
        print(f"缺少環境變數：{name}")
        sys.exit(1)
    return val


def send_telegram(bot_token: str, chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    # Telegram 單則訊息長度限制約 4096 字元，過長就切段送出
    for i in range(0, len(text), 3900):
        chunk = text[i:i + 3900]
        payload = json.dumps({"chat_id": chat_id, "text": chunk}).encode("utf-8")
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


def load_state() -> dict:
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"last_update_id": 0, "stocks": {}}


def save_state(state: dict):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ========== 到價提醒部分 ==========

def load_watch_stocks(csv_url: str) -> list:
    req = urllib.request.Request(csv_url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8-sig")

    reader = csv.reader(io.StringIO(raw))
    rows = list(reader)
    if not rows:
        return []

    stocks = []
    for row in rows[1:]:
        if not row or not row[0].strip():
            continue
        code = row[0].strip()
        market = (row[1].strip().lower() if len(row) > 1 and row[1].strip() else "tse")
        above_raw = row[2].strip() if len(row) > 2 else ""
        below_raw = row[3].strip() if len(row) > 3 else ""
        try:
            above = float(above_raw) if above_raw else None
        except ValueError:
            above = None
        try:
            below = float(below_raw) if below_raw else None
        except ValueError:
            below = None
        stocks.append({"code": code, "market": market, "above": above, "below": below})
    return stocks


def fetch_realtime_price(stock_code: str, market: str):
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
        raise RuntimeError(f"查無股票資料：{data}")
    info = arr[0]
    price_str = info.get("z")
    if price_str in (None, "-", ""):
        price_str = info.get("b", "").split("_")[0] or info.get("y")
    if not price_str or price_str == "-":
        raise RuntimeError(f"查無有效成交價：{info}")
    return float(price_str), info.get("n", stock_code)


def run_price_alerts(bot_token, chat_id, csv_url, state):
    try:
        stocks = load_watch_stocks(csv_url)
    except Exception as e:
        print(f"讀取股票清單失敗：{e}")
        return

    for s in stocks:
        code, market = s["code"], s["market"]
        target_above, target_below = s["above"], s["below"]
        if target_above is None and target_below is None:
            continue

        st = state["stocks"].setdefault(
            code, {"acknowledged_above": False, "acknowledged_below": False}
        )
        try:
            price, name = fetch_realtime_price(code, market)
        except Exception as e:
            print(f"{code} 抓取股價失敗：{e}")
            continue

        print(f"[到價監控] {name}({code}) 現價：{price}")

        hit_above = target_above is not None and price >= target_above
        hit_below = target_below is not None and price <= target_below

        if target_above is not None and price < target_above:
            st["acknowledged_above"] = False
        if target_below is not None and price > target_below:
            st["acknowledged_below"] = False

        if hit_above and not st["acknowledged_above"]:
            send_telegram(bot_token, chat_id,
                f"📈 {name}({code}) 已漲到 {price} 元\n目標價（漲）：{target_above}\n\n"
                f"回覆「{code} 謝謝」即可停止此提醒")
            print(f"{code}：已發送漲到通知。")

        if hit_below and not st["acknowledged_below"]:
            send_telegram(bot_token, chat_id,
                f"📉 {name}({code}) 已跌到 {price} 元\n目標價（跌）：{target_below}\n\n"
                f"回覆「{code} 謝謝」即可停止此提醒")
            print(f"{code}：已發送跌到通知。")


def is_ack_message(text: str, stock_code: str) -> bool:
    if not text:
        return False
    return re.search(rf"{re.escape(stock_code)}\s*[，,]?\s*謝謝", text) is not None


# ========== 隨選歷史分析部分 ==========

ANALYZE_PATTERN = re.compile(r"^(?:分析|analyze)\s*([A-Za-z0-9]+)\s*$", re.IGNORECASE)


def compute_rsi(close, period=14):
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(period).mean()
    avg_loss = loss.rolling(period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_bollinger(close, window=20, num_std=2.0):
    ma = close.rolling(window).mean()
    std = close.rolling(window).std()
    return ma, ma + num_std * std, ma - num_std * std


def find_price_clusters(close, num_bins=60, top_n=8):
    min_p, max_p = close.min(), close.max()
    if max_p <= min_p:
        return []
    bins = np.linspace(min_p, max_p, num_bins + 1)
    hist, edges = np.histogram(close, bins=bins)
    order = np.argsort(hist)[::-1][:top_n]
    levels = [{"level": round(float((edges[i] + edges[i + 1]) / 2), 2), "touches": int(hist[i])} for i in order]
    levels.sort(key=lambda x: x["level"])
    return levels


def backtest_level_reaction(close, level, tolerance=0.02, forward_days=10):
    lower, upper = level * (1 - tolerance), level * (1 + tolerance)
    touched = close[(close >= lower) & (close <= upper)]
    idx_list = close.index.tolist()
    returns = []
    for date in touched.index:
        i = idx_list.index(date)
        if i + forward_days < len(close):
            returns.append(close.iloc[i + forward_days] / close.iloc[i] - 1)
    if not returns:
        return None
    returns = np.array(returns)
    return {
        "n": len(returns),
        "avg": round(float(returns.mean()) * 100, 2),
        "win_rate": round(float((returns > 0).mean()) * 100, 1),
    }


def resolve_ticker_and_fetch(raw_code: str):
    """依序嘗試 .TW / .TWO / 原樣（美股），回傳 (yfinance ticker字串, close序列)"""
    candidates = []
    if raw_code.isdigit():
        candidates = [f"{raw_code}.TW", f"{raw_code}.TWO"]
    else:
        candidates = [raw_code]

    for ticker in candidates:
        try:
            df = yf.download(ticker, period="1100d", progress=False, auto_adjust=True)
            if isinstance(df.columns, type(df.columns)) and hasattr(df.columns, "get_level_values"):
                try:
                    df.columns = df.columns.get_level_values(0)
                except Exception:
                    pass
            if not df.empty and "Close" in df:
                close = df["Close"].dropna()
                if len(close) > 30:
                    return ticker, close
        except Exception as e:
            print(f"嘗試 {ticker} 失敗：{e}")
    return None, None


def build_analysis_report(raw_code: str) -> str:
    if yf is None:
        return "分析功能目前無法使用（缺少 yfinance 套件），請聯絡管理者確認 workflow 設定。"

    ticker, close = resolve_ticker_and_fetch(raw_code)
    if close is None:
        return f"查無「{raw_code}」的歷史資料，請確認代號是否正確。"

    current_price = float(close.iloc[-1])
    ma20 = close.rolling(20).mean().iloc[-1]
    ma60 = close.rolling(60).mean().iloc[-1]
    rsi = compute_rsi(close).iloc[-1]
    _, bb_upper, bb_lower = compute_bollinger(close)

    lookback = min(252, len(close))
    recent = close.iloc[-lookback:]
    swing_high, swing_low = float(recent.max()), float(recent.min())

    clusters = find_price_clusters(close, top_n=8)
    scored = []
    for c in clusters:
        stat = backtest_level_reaction(close, c["level"])
        scored.append((c["level"], c["touches"], stat))

    supports = sorted([x for x in scored if x[0] < current_price], key=lambda x: -x[0])[:2]
    resistances = sorted([x for x in scored if x[0] > current_price], key=lambda x: x[0])[:2]

    lines = [
        f"📊 {ticker} 歷史分析報告",
        f"現價：{current_price:.2f}",
        f"MA20：{ma20:.2f}｜MA60：{ma60:.2f}",
        f"RSI(14)：{rsi:.1f}" + ("（超買）" if rsi > 70 else "（超賣）" if rsi < 30 else "（中性）"),
        f"布林通道：上{bb_upper.iloc[-1]:.2f}／下{bb_lower.iloc[-1]:.2f}",
        f"近一年高低：{swing_high:.2f} / {swing_low:.2f}",
        "",
        "可能支撐（歷史回測10日後表現）：",
    ]
    if supports:
        for level, touches, stat in supports:
            if stat and stat["n"] >= 3:
                lines.append(f"  {level}：上漲機率{stat['win_rate']}%，平均{stat['avg']:+.2f}%（樣本{stat['n']}）")
            else:
                lines.append(f"  {level}：樣本太少，僅供參考")
    else:
        lines.append("  （無明顯資料）")

    lines.append("")
    lines.append("可能壓力（歷史回測10日後表現）：")
    if resistances:
        for level, touches, stat in resistances:
            if stat and stat["n"] >= 3:
                lines.append(f"  {level}：上漲機率{stat['win_rate']}%，平均{stat['avg']:+.2f}%（樣本{stat['n']}）")
            else:
                lines.append(f"  {level}：樣本太少，僅供參考")
    else:
        lines.append("  （無明顯資料）")

    lines.append("")
    lines.append("⚠️ 僅為歷史統計，不構成投資建議，不保證未來重演。")

    return "\n".join(lines)


# ========== 主流程 ==========

def main():
    bot_token = get_env("BOT_TOKEN")
    chat_id = get_env("CHAT_ID")
    csv_url = os.environ.get("STOCKS_CSV_URL", "")

    state = load_state()
    state.setdefault("stocks", {})

    updates = fetch_telegram_updates(bot_token, state.get("last_update_id", 0))
    analyze_requests = []

    for update in updates:
        state["last_update_id"] = max(state.get("last_update_id", 0), update["update_id"])
        text = update.get("message", {}).get("text", "").strip()

        m = ANALYZE_PATTERN.match(text)
        if m:
            analyze_requests.append(m.group(1))
            continue

        # 確認訊息："<代號> 謝謝"
        for code, st in state["stocks"].items():
            if is_ack_message(text, code):
                st["acknowledged_above"] = True
                st["acknowledged_below"] = True
                print(f"{code} 已確認，停止通知。")

    # 處理隨選分析請求
    for raw_code in analyze_requests:
        print(f"收到分析請求：{raw_code}")
        try:
            report = build_analysis_report(raw_code)
        except Exception as e:
            report = f"分析「{raw_code}」時發生錯誤：{e}"
        send_telegram(bot_token, chat_id, report)

    # 處理到價監控（沒設定 CSV 連結就跳過這部分）
    if csv_url:
        run_price_alerts(bot_token, chat_id, csv_url, state)
    else:
        print("未設定 STOCKS_CSV_URL，略過到價監控。")

    save_state(state)


if __name__ == "__main__":
    main()
