import os
import time

import requests
import pandas as pd
import telebot

# Token: ưu tiên biến môi trường TELEGRAM_TOKEN, nếu không có thì dùng token mặc định
TOKEN = os.getenv("TELEGRAM_TOKEN") or "8467448818:AAG77dVAILlF2i19weMEDBhcvuvdrcJF3co"
bot = telebot.TeleBot(TOKEN)

BASE_URL = "https://services.entrade.com.vn/chart-api/v2/ohlcs"
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def fetch_stock_data(symbol, days=420):
    """Lấy dữ liệu ngày của VNINDEX hoặc cổ phiếu, trả về DataFrame."""
    symbol = symbol.upper()
    kind = "index" if symbol == "VNINDEX" else "stock"
    now = int(time.time())
    params = {
        "from": now - days * 86400,
        "to": now,
        "symbol": symbol,
        "resolution": "1D",
    }

    last_err = None
    for _ in range(3):  # thử lại tối đa 3 lần
        try:
            r = requests.get(f"{BASE_URL}/{kind}", params=params,
                             headers=HEADERS, timeout=15)
            r.raise_for_status()
            d = r.json()
            df = pd.DataFrame({
                "date": pd.to_datetime(d["t"], unit="s"),
                "open": d["o"], "high": d["h"], "low": d["l"],
                "close": d["c"], "volume": d["v"],
            })
            if df.empty:
                raise ValueError("API trả về dữ liệu rỗng")
            return df.sort_values("date").reset_index(drop=True)
        except Exception as e:
            last_err = e
            time.sleep(2)
    raise RuntimeError(f"Không lấy được dữ liệu cho {symbol}: {last_err}")


def add_indicators(df):
    df["MA50"] = df["close"].rolling(50).mean()
    df["MA200"] = df["close"].rolling(200).mean()
    df["Vol_MA20"] = df["volume"].rolling(20).mean()

    # CMDX: dao động dòng tiền 14 phiên, thang 0-100 (bản chất là Money Flow Index).
    # Dòng tiền thô = giá điển hình x khối lượng; tính là dòng VÀO nếu đóng cửa tăng
    # so với phiên trước, dòng RA nếu giảm.
    typical_price = (df["high"] + df["low"] + df["close"]) / 3
    raw_money_flow = typical_price * df["volume"]
    diff = df["close"].diff()

    pos_sum = raw_money_flow.where(diff > 0, 0.0).rolling(14).sum()
    neg_sum = raw_money_flow.where(diff < 0, 0.0).rolling(14).sum()
    total = pos_sum + neg_sum

    cmdx = 100 * pos_sum / total            # = 100 - 100/(1 + pos/neg)
    df["CMDX"] = cmdx.where(total != 0, 50)  # không có dòng tiền -> trung lập 50
    return df


def cmdx_label(v):
    if v > 60:
        return "Mạnh (Dòng tiền áp đảo Mua)"
    if v < 40:
        return "Yếu (Dòng tiền nghiêng về Bán)"
    return "Trung lập / Giằng co"


def fmt_price(x):
    """Tự nhận biết đơn vị: giá nhỏ (nghìn đồng) hiện 2 số lẻ, giá lớn hiện nguyên."""
    return f"{x:,.2f}" if abs(x) < 1000 else f"{x:,.0f}"


def safe_send(chat_id, text):
    """Gửi Markdown, nếu Telegram từ chối thì gửi lại dạng văn bản thường."""
    try:
        bot.send_message(chat_id, text, parse_mode="Markdown")
    except Exception:
        bot.send_message(chat_id, text.replace("*", ""))


# ---------- Lệnh: tttl (trạng thái VN-Index tích hợp CMDX) ----------
@bot.message_handler(func=lambda m: m.text and m.text.strip().lower() == "tttl")
def send_tttl_report(message):
    bot.reply_to(message, "Đang lấy dữ liệu VN-Index và đo lường dòng tiền CMDX...")
    try:
        df = add_indicators(fetch_stock_data("VNINDEX"))
        if len(df) < 200:
            safe_send(message.chat.id, "❌ Không đủ dữ liệu lịch sử để tính toán.")
            return

        latest, prev = df.iloc[-1], df.iloc[-2]
        change = latest["close"] - prev["close"]
        pct = change / prev["close"] * 100

        trend_ma50 = "Nằm trên" if latest["close"] > latest["MA50"] else "Nằm dưới"
        trend_ma200 = "Nằm trên" if latest["close"] > latest["MA200"] else "Nằm dưới"
        vol_status = "Cao hơn" if latest["volume"] > latest["Vol_MA20"] else "Thấp hơn"
        cmdx_val = latest["CMDX"]

        report = (
            "📊 *[tttl] TRẠNG THÁI THỊ TRƯỜNG & DÒNG TIỀN - VNINDEX* 📊\n"
            f"• Ngày dữ liệu: {latest['date']:%d/%m/%Y}\n"
            f"• Điểm số hiện tại: {latest['close']:.2f} ({change:+.2f} | {pct:+.2f}%)\n"
            f"• Thanh khoản: {vol_status} trung bình 20 phiên\n"
            f"• Chỉ báo dòng tiền CMDX: *{cmdx_val:.1f} điểm* ({cmdx_label(cmdx_val)})\n\n"
            "📈 *Kỹ thuật cơ sở:*\n"
            f"• So với MA50 ({latest['MA50']:.2f}): {trend_ma50}\n"
            f"• So với MA200 ({latest['MA200']:.2f}): {trend_ma200}"
        )
        safe_send(message.chat.id, report)
    except Exception as e:
        print("[LỖI tttl]", e)
        bot.send_message(message.chat.id, "Đã xảy ra lỗi khi lấy dữ liệu: " + str(e))


# ---------- Lệnh: /scan [mã] ----------
@bot.message_handler(commands=["scan"])
def scan_stock(message):
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "⚠️ Vui lòng nhập mã cổ phiếu. Ví dụ: /scan acb hoặc /scan vcg")
        return

    ticker = args[1].upper()
    if not (ticker.isalnum() and 2 <= len(ticker) <= 10):
        bot.reply_to(message, "❌ Mã cổ phiếu không hợp lệ.")
        return

    bot.reply_to(message, f"🔍 Đang quét cấu trúc kỹ thuật cho mã {ticker}...")

    try:
        df = fetch_stock_data(ticker)
        if len(df) < 200:
            safe_send(message.chat.id, f"❌ Mã {ticker} không đủ dữ liệu lịch sử (cần ≥ 200 phiên).")
            return

        df = add_indicators(df)
        latest, prev = df.iloc[-1], df.iloc[-2]
        change = latest["close"] - prev["close"]
        pct = change / prev["close"] * 100

        trend_ma50 = "Nằm trên" if latest["close"] > latest["MA50"] else "Nằm dưới"
        trend_ma200 = "Nằm trên" if latest["close"] > latest["MA200"] else "Nằm dưới"
        vol_status = "Cao hơn" if latest["volume"] > latest["Vol_MA20"] else "Thấp hơn"
        cmdx_val = latest["CMDX"]

        report = (
            f"🎯 *[SCANNER] KẾT QUẢ PHÂN TÍCH: {ticker}* 🎯\n"
            f"• Ngày dữ liệu: {latest['date']:%d/%m/%Y}\n"
            f"• Giá hiện tại: {fmt_price(latest['close'])} ({change:+,.2f} | {pct:+.2f}%)\n"
            f"• Khối lượng khớp: {latest['volume']:,.0f} ({vol_status} trung bình 20 phiên)\n"
            f"• Dòng tiền CMDX: *{cmdx_val:.1f}* ({cmdx_label(cmdx_val)})\n\n"
            "📈 *Trạng thái kỹ thuật:*\n"
            f"• So với MA50 ({fmt_price(latest['MA50'])}): *{trend_ma50}*\n"
            f"• So với MA200 ({fmt_price(latest['MA200'])}): *{trend_ma200}*"
        )
        safe_send(message.chat.id, report)
    except Exception as e:
        print(f"[LỖI scan {ticker}]", e)
        bot.send_message(message.chat.id,
                         f"❌ Không lấy được dữ liệu mã {ticker}. Kiểm tra lại mã hoặc thử lại sau.")


if __name__ == "__main__":
    print("Forecaster Terminal (Market + Stock Screener) đã khởi động. Đang lắng nghe lệnh...")
    bot.infinity_polling()