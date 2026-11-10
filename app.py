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

# تنظیمات اسکلپ
LEVERAGE = 15
TARGET_MARGIN = 0.2
TAKE_PROFIT_PCT = 0.008  # 0.8% حرکت قیمت
STOP_LOSS_PCT = 0.004    # 0.4% حرکت قیمت

# متغیرهای کنترل برای جلوگیری از ترید تکراری و عجله
last_traded_symbol = None
last_trade_direction = None
last_trade_time = 0
MIN_SECONDS_BETWEEN_TRADES = 45  # حداقل فاصله بین دو معامله موفق

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
        requests.post(url, data=payload, timeout=8)
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

def fetch_and_resample_candles(exchange, symbol):
    """دریافت کندل‌های ۵ دقیقه و ساخت کندل‌های ۱۰ دقیقه"""
    try:
        ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=60)
        if len(ohlcv_5m) < 30:
            return None, None
            
        df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_5m['timestamp'] = pd.to_datetime(df_5m['timestamp'], unit='ms')
        df_5m.set_index('timestamp', inplace=True)
        
        df_10m = df_5m.resample('10min').agg({
            'open': 'first',
            'high': 'max',
            'low': 'min',
            'close': 'last',
            'volume': 'sum'
        }).dropna().reset_index()
        
        df_5m = df_5m.reset_index()
        return df_5m, df_10m
    except Exception as e:
        print(f"Candle Fetch Error for {symbol}: {e}")
        return None, None

def calculate_fibonacci_levels(df):
    """محاسبه سطوح فیبوناچی اصلاحی"""
    high = df['high'].max()
    low = df['low'].min()
    diff = high - low
    if diff == 0:
        return None, None, high, low
        
    long_levels = {
        '0.382': high - 0.382 * diff,
        '0.5': high - 0.5 * diff,
        '0.618': high - 0.618 * diff
    }
    short_levels = {
        '0.382': low + 0.382 * diff,
        '0.5': low + 0.5 * diff,
        '0.618': low + 0.618 * diff
    }
    return long_levels, short_levels, high, low

def calculate_rsi(series, period=14):
    """محاسبه اندیکاتور RSI"""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def place_tp_sl_orders(exchange, symbol, side, amount, entry_price):
    """ثبت مطمئن سفارش‌های حد سود و حد ضرر با چند بار تلاش"""
    if side == 'buy':
        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 - STOP_LOSS_PCT)
        tp_side = 'sell'
        sl_side = 'sell'
    else:
        tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 + STOP_LOSS_PCT)
        tp_side = 'buy'
        sl_side = 'buy'

    tp_ok = False
    sl_ok = False

    for attempt in range(3):
        try:
            if not tp_ok:
                exchange.create_order(
                    symbol, 'take_profit_market', tp_side, amount, None,
                    {'stopPrice': tp_price, 'reduceOnly': True}
                )
                tp_ok = True
            if not sl_ok:
                exchange.create_order(
                    symbol, 'stop_market', sl_side, amount, None,
                    {'stopPrice': sl_price, 'reduceOnly': True}
                )
                sl_ok = True
            if tp_ok and sl_ok:
                return True, tp_price, sl_price
        except Exception as e:
            print(f"TP/SL attempt {attempt+1} failed for {symbol}: {e}")
            time.sleep(1.5)

    return False, tp_price, sl_price

def run_bot_loop():
    global last_traded_symbol, last_trade_direction, last_trade_time

    time.sleep(10)
    send_telegram("🚀 <b>ربات اسکالپ با کنترل شدوها، فیلتر ترید تکراری و مانیتورینگ لحظه‌ای استارت شد!</b>")
    
    exchange = get_toobit_exchange()
    try:
        exchange.load_markets()
        balance = exchange.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0)
        send_telegram(f"✅ اتصال برقرار شد.\n💰 موجودی آزاد: <b>{usdt_free} USDT</b>")
    except Exception as e:
        send_telegram(f"❌ خطا در اتصال: {e}")
        return
        
    last_status_report = 0
    previous_active_count = 0

    while True:
        try:
            positions = exchange.fetch_positions()
            active_positions = [p for p in positions if float(p.get('contracts', 0) or 0) > 0]
            current_active_count = len(active_positions)

            # ========== مانیتورینگ پوزیشن‌های باز ==========
            if current_active_count > 0:
                # ارسال گزارش دوره‌ای (هر ۲ دقیقه یکبار)
                if time.time() - last_status_report > 120:
                    msg = "📊 <b>وضعیت فعلی پوزیشن‌ها:</b>\n"
                    for p in active_positions:
                        symbol = p.get('symbol', 'N/A')
                        side = p.get('side', 'N/A')
                        contracts = float(p.get('contracts', 0) or 0)
                        entry = float(p.get('entryPrice', 0) or 0)
                        unrealized = float(p.get('unrealizedPnl', 0) or 0)
                        msg += f"• {symbol} | {side} | ورود: {entry:.4f} | سود/ضرر: <b>{unrealized:.4f}</b>\n"
                    send_telegram(msg)
                    last_status_report = time.time()

                # تشخیص بسته شدن پوزیشن
                if previous_active_count > 0 and current_active_count < previous_active_count:
                    send_telegram("🔔 <b>یک یا چند پوزیشن بسته شد.</b>\nدر حال اسکن مجدد کل بازار برای بهترین فرصت...")
                    # بعد از بسته شدن، کمی صبر کن و دوباره اسکن کن
                    time.sleep(20)

                previous_active_count = current_active_count
                time.sleep(12)
                continue

            # اگر پوزیشنی باز نیست
            if previous_active_count > 0 and current_active_count == 0:
                send_telegram("✅ همه پوزیشن‌ها بسته شدند. شروع اسکن کامل بازار...")
                previous_active_count = 0

            # جلوگیری از عجله بعد از معامله قبلی
            if time.time() - last_trade_time < MIN_SECONDS_BETWEEN_TRADES:
                time.sleep(10)
                continue

            # ========== اسکن بازار ==========
            tickers = exchange.fetch_tickers()
            candidates = []
            
            for symbol, data in tickers.items():
                # اولویت پایین به آخرین نماد معامله‌شده (ولی کاملاً بلاک نمی‌کنیم)
                if symbol == last_traded_symbol:
                    continue
                if '/USDT' in symbol:
                    vol = data.get('quoteVolume', 0) or 0
                    change = data.get('percentage', 0) or 0
                    if vol > 50000:
                        candidates.append((symbol, change, vol))
                        
            candidates.sort(key=lambda x: abs(x[1]), reverse=True)
            
            if not candidates:
                time.sleep(25)
                continue
                
            traded_successfully = False
            
            for symbol, change, vol in candidates[:15]:
                try:
                    df_5m, df_10m = fetch_and_resample_candles(exchange, symbol)
                    if df_5m is None or len(df_5m) < 20 or df_10m is None or len(df_10m) < 10:
                        continue
                        
                    # --- بررسی سقف، کف و شدوهای کندل‌های قبلی در ۵ دقیقه ---
                    prev_high = df_5m['high'].iloc[-3]
                    prev_low = df_5m['low'].iloc[-3]
                    c_close_5m = df_5m['close'].iloc[-2]
                    
                    # --- تحلیل تایم‌فریم ۵ دقیقه ---
                    df_5m['ema5'] = df_5m['close'].ewm(span=5, adjust=False).mean()
                    df_5m['ema13'] = df_5m['close'].ewm(span=13, adjust=False).mean()
                    df_5m['rsi'] = calculate_rsi(df_5m['close'], period=14)
                    
                    c_rsi_5m = df_5m['rsi'].iloc[-2]
                    ema5_5m = df_5m['ema5'].iloc[-2]
                    ema13_5m = df_5m['ema13'].iloc[-2]
                    
                    long_fib_5m, short_fib_5m, _, _ = calculate_fibonacci_levels(df_5m)
                    
                    # --- تحلیل تایم‌فریم ۱۰ دقیقه (بررسی شدوها و ریجکت قیمت) ---
                    c_10m = df_10m.iloc[-2]
                    o_10m, h_10m, l_10m, cls_10m = c_10m['open'], c_10m['high'], c_10m['low'], c_10m['close']
                    range_10m = h_10m - l_10m
                    
                    upper_wick_10m = (h_10m - max(o_10m, cls_10m)) / range_10m if range_10m > 0 else 0
                    lower_wick_10m = (min(o_10m, cls_10m) - l_10m) / range_10m if range_10m > 0 else 0
                    
                    side = None
                    
                    # ==========================================
                    # سیستم بررسی شرط‌ها (تشخیص جهت معامله)
                    # ==========================================
                    
                    # بررسی برخورد قیمت به سقف یا کف شدوهای کندل‌های قبلی
                    if c_close_5m <= prev_low * 1.002:
                        side = 'buy'
                    elif c_close_5m >= prev_high * 0.998:
                        side = 'sell'
                        
                    # کراس EMA در ۵ دقیقه
                    elif ema5_5m > ema13_5m and df_5m['ema5'].iloc[-3] <= df_5m['ema13'].iloc[-3]:
                        side = 'buy'
                    elif ema5_5m < ema13_5m and df_5m['ema5'].iloc[-3] >= df_5m['ema13'].iloc[-3]:
                        side = 'sell'
                        
                    # برخورد با فیبوناچی
                    elif long_fib_5m and long_fib_5m['0.618'] <= c_close_5m <= long_fib_5m['0.382']:
                        side = 'buy'
                    elif short_fib_5m and short_fib_5m['0.382'] <= c_close_5m <= short_fib_5m['0.618']:
                        side = 'sell'
                        
                    # شدوی ۱۰ دقیقه
                    elif lower_wick_10m >= 0.35:
                        side = 'buy'
                    elif upper_wick_10m >= 0.35:
                        side = 'sell'
                        
                    # اشباع RSI
                    elif c_rsi_5m < 30:
                        side = 'buy'
                    elif c_rsi_5m > 70:
                        side = 'sell'
                    if not side:
                        continue
                        
                    # ========== فیلتر جلوگیری از ترید همجهت پشت‌سرهم روی همان ارز ==========
                    if symbol == last_traded_symbol and side == last_trade_direction:
                        continue
                        
                    try:
                        exchange.set_leverage(LEVERAGE, symbol)
                    except:
                        pass
                        
                    market = exchange.market(symbol)
                    min_cost = market.get('limits', {}).get('cost', {}).get('min', 1.0) or 1.0
                    effective_cost = max(TARGET_MARGIN * LEVERAGE, min_cost)
                    
                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    amount = effective_cost / price
                    amount = exchange.amount_to_precision(symbol, amount)
                    
                    if float(amount) <= 0:
                        continue
                        
                    # ثبت سفارش بازار
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, amount)
                    else:
                        order = exchange.create_market_sell_order(symbol, amount)

                    entry_price = float(order.get('average') or order.get('price') or price)

                    # ثبت مطمئن TP و SL
                    tp_sl_success, tp_price, sl_price = place_tp_sl_orders(
                        exchange, symbol, side, amount, entry_price
                    )

                    if not tp_sl_success:
                        # اگر TP/SL ثبت نشد، سعی می‌کنیم پوزیشن را ببندیم تا بدون محافظت نماند
                        try:
                            if side == 'buy':
                                exchange.create_market_sell_order(symbol, amount, None, {'reduceOnly': True})
                            else:
                                exchange.create_market_buy_order(symbol, amount, None, {'reduceOnly': True})
                            send_telegram(
                                f"⚠️ <b>هشدار:</b> پوزیشن {symbol} باز شد اما TP/SL ثبت نشد.\n"
                                f"پوزیشن به صورت اضطراری بسته شد تا بدون محافظت نماند."
                            )
                            continue
                        except Exception as close_err:
                            send_telegram(
                                f"❌ <b>خطای بحرانی:</b> پوزیشن {symbol} باز شد و TP/SL هم ثبت نشد و بستن هم ممکن نشد!\n"
                                f"لطفاً دستی چک کنید. خطا: {close_err}"
                            )
                            continue

                    # ارسال گزارش کامل باز شدن پوزیشن
                    send_telegram(
                        f"🎯 <b>پوزیشن جدید باز شد (با TP و SL تأییدشده)</b>\n"
                        f"نماد: <b>{symbol}</b>\n"
                        f"جهت: <b>{'خرید (Long)' if side == 'buy' else 'فروش (Short)'}</b>\n"
                        f"قیمت ورود: <b>{entry_price:.6f}</b>\n"
                        f"حد سود: <b>{tp_price:.6f}</b>\n"
                        f"حد ضرر: <b>{sl_price:.6f}</b>\n"
                        f"اهرم: {LEVERAGE}x"
                    )
                    
                    last_traded_symbol = symbol
                    last_trade_direction = side
                    last_trade_time = time.time()
                    traded_successfully = True
                    previous_active_count = 1  # برای مانیتورینگ بعدی
                    break
                    
                except Exception as ex:
                    print(f"Error processing {symbol}: {ex}")
                    continue
                    
            if not traded_successfully:
                time.sleep(25)
                
        except Exception as e:
            print(f"Loop Error: {e}")
            send_telegram(f"⚠️ خطای حلقه اصلی: {e}")
            time.sleep(15)

@app.route('/')
def home():
    return "Scalp Bot with Wick Testing, Anti-Repeat Protection & Real-time Monitoring is running!"

if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
