import os
import time
import threading
import requests
import ccxt
import pandas as pd
from flask import Flask

app = Flask(__name__)

# تنظیمات از متغیرهای محیطی Render
API_KEY = os.environ.get("TOOBIT_API_KEY", "")
SECRET_KEY = os.environ.get("TOOBIT_SECRET_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# تنظیمات ترید (مارجین کاهش یافته به 0.3 دلار)
LEVERAGE = 15
TARGET_MARGIN = 0.3  # مارجین هدف جدید (دلار)
TAKE_PROFIT_PCT = 0.10  # 10% سود
STOP_LOSS_PCT = 0.05   # 5% ضرر


def send_telegram(message):
    """ارسال گزارش به تلگرام"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Telegram Mock] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML"
        }
        requests.post(url, data=payload, timeout=5)
    except Exception as e:
        print(f"Telegram Error: {e}")


def get_toobit_exchange():
    exchange = ccxt.toobit({
        'apiKey': API_KEY,
        'secret': SECRET_KEY,
        'enableRateLimit': True,
        'options': {'defaultType': 'swap'}  # فیوچرز
    })
    return exchange


def run_bot_loop():
    time.sleep(10)  # صبر برای بالا آمدن کامل سرور
    send_telegram("🚀 <b>ربات تریدر توبیت استارت شد!</b>\nدر حال بررسی بازار و جستجوی فرصت مناسب...")

    exchange = get_toobit_exchange()

    try:
        exchange.load_markets()
        balance = exchange.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0)
        send_telegram(f"✅ اتصال به صرافی برقرار شد.\n💰 موجودی آزاد حساب: <b>{usdt_free} USDT</b>")
    except Exception as e:
        send_telegram(f"❌ <b>خطا در اتصال به صرافی توبیت:</b>\n{e}")
        return

    last_traded_symbol = None

    while True:
        try:
            # بررسی پوزیشن‌های باز فعلی
            positions = exchange.fetch_positions()
            active_positions = [p for p in positions if float(p.get('contracts', 0) or 0) > 0]

            if len(active_positions) > 0:
                # اگر پوزیشن باز داریم، صبر می‌کنیم تا بسته شود
                time.sleep(15)
                continue

            # اسکن بازار و فیلتر ارزهای پرنوسان و پرحجم
            tickers = exchange.fetch_tickers()
            candidates = []

            for symbol, data in tickers.items():
                if symbol == last_traded_symbol:
                    continue
                if '/USDT' in symbol:
                    vol = data.get('quoteVolume', 0) or 0
                    change = data.get('percentage', 0) or 0
                    if vol > 50000:  # حجم بالای 50 هزار دلار
                        candidates.append((symbol, change, vol))

            # مرتب‌سازی بر اساس بیشترین نوسان
            candidates.sort(key=lambda x: abs(x[1]), reverse=True)

            if not candidates:
                time.sleep(20)
                continue

            traded_successfully = False

            # بررسی ارزها به نوبت
            for symbol, change, vol in candidates[:15]:
                try:
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=20)
                    if len(ohlcv) < 15:
                        continue

                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
                    df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()

                    prev_ema5 = df['ema5'].iloc[-2]
                    prev_ema13 = df['ema13'].iloc[-2]
                    curr_ema5 = df['ema5'].iloc[-1]
                    curr_ema13 = df['ema13'].iloc[-1]

                    side = None
                    if prev_ema5 <= prev_ema13 and curr_ema5 > curr_ema13:
                        side = 'buy'   # لانگ
                    elif prev_ema5 >= prev_ema13 and curr_ema5 < curr_ema13:
                        side = 'sell'  # شورت

                    if not side:
                        # اگر سیگنالی نبود، هیچ معامله‌ای اجباری باز نمی‌شود و رد می‌شود
                        continue

                    # تنظیم لوریج
                    try:
                        exchange.set_leverage(LEVERAGE, symbol)
                    except:
                        pass

                    # بررسی حداقل مقدار مجاز صرافی برای جلوگیری از خطای حجم
                    market = exchange.market(symbol)
                    min_cost = market.get('limits', {}).get('cost', {}).get('min', 1.0) or 1.0
                    effective_cost = max(TARGET_MARGIN * LEVERAGE, min_cost)

                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    amount = effective_cost / price
                    amount = exchange.amount_to_precision(symbol, amount)

                    if float(amount) <= 0:
                        continue

                    # ارسال سفارش به صرافی همراه با گزارش دقیق در تلگرام
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, float(amount))
                        send_telegram(
                            f"🟢 <b>پوزیشن LONG (خرید) باز شد!</b>\n"
                            f"📌 ارز: <b>{symbol}</b>\n"
                            f"💲 قیمت ورود: <code>{price}</code>\n"
                            f"📊 حجم مارجین: <b>{TARGET_MARGIN}$</b> (با لوریج {LEVERAGE}x)\n"
                            f"📈 تغییرات حجم بازار: {vol:,.0f}$"
                        )
                    else:
                        order = exchange.create_market_sell_order(symbol, float(amount))
                        send_telegram(
                            f"🔴 <b>پوزیشن SHORT (فروش) باز شد!</b>\n"
                            f"📌 ارز: <b>{symbol}</b>\n"
                            f"💲 قیمت ورود: <code>{price}</code>\n"
                            f"📊 حجم مارجین: <b>{TARGET_MARGIN}$</b> (با لوریج {LEVERAGE}x)\n"
                            f"📉 تغییرات حجم بازار: {vol:,.0f}$"
                        )

                    last_traded_symbol = symbol
                    traded_successfully = True
                    break  # پوزیشن باز شد، از حلقه ارزها خارج می‌شویم

                except Exception as sub_e:
                    continue

            if not traded_successfully:
                # اگر در این چرخه فرصتی پیدا نشد، ربات منتظر می‌ماند و اجباری خرید نمی‌کند
                time.sleep(30)

        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(15)


@app.route('/')
def home():
    return "Toobit Trading Bot is running 24/7 with Telegram & Auto-Skip!"


if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop, daemon=True)
    t.start()

    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
