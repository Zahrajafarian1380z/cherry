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
MAX_MARGIN = 0.3
TAKE_PROFIT_PCT = 0.10 / LEVERAGE   # ۱۰٪ سود با اهرم
STOP_LOSS_PCT   = 0.05 / LEVERAGE   # ۵٪ ضرر با اهرم

# === استاپ جدید (درخواست تو) ===
SOFT_STOP_PCT   = 0.12 / LEVERAGE   # ۱۲٪ → شروع مانیتورینگ (هنوز نبنده)
RECOVERY_PCT    = 0.09 / LEVERAGE   # ۹٪ → بستن در قیمت بهتر
FORCE_STOP_PCT  = 0.17 / LEVERAGE   # ۱۷٪ → بستن اجباری فوری

# متغیرهای کنترل
last_traded_symbol = None
last_trade_direction = None
last_trade_time = 0
MIN_SECONDS_BETWEEN_TRADES = 45

# وضعیت پوزیشن‌هایی که به ۱۲٪ رسیدن و دارن برای قیمت بهتر صبر می‌کنن
positions_in_soft_stop = {}   # symbol -> True

def send_telegram(message):
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
        'options': {
            'defaultType': 'swap',
            'defaultMarginMode': 'isolated'
        }
    })
    return exchange

def set_isolated_mode(exchange, symbol):
    try:
        exchange.set_margin_mode('isolated', symbol)
        print(f"✅ مود ایزوله برای {symbol} تنظیم شد")
        return True
    except Exception as e1:
        print(f"set_margin_mode failed: {e1}")
        try:
            exchange.set_leverage(LEVERAGE, symbol, params={'marginMode': 'isolated'})
            print(f"✅ مود ایزوله از طریق set_leverage برای {symbol} تنظیم شد")
            return True
        except Exception as e2:
            print(f"Fallback set_leverage also failed: {e2}")
            return False

def fetch_and_resample_candles(exchange, symbol):
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
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr(df, period=14):
    """محاسبه ATR ساده"""
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr

def place_tp_sl_orders(exchange, symbol, side, amount, entry_price):
    if side == 'buy' or side == 'LONG':
        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 - STOP_LOSS_PCT)
        close_side = 'sell'
    else:
        tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 + STOP_LOSS_PCT)
        close_side = 'buy'

    try:
        tp_price = float(exchange.price_to_precision(symbol, tp_price))
        sl_price = float(exchange.price_to_precision(symbol, sl_price))
        amount = float(exchange.amount_to_precision(symbol, amount))
    except:
        pass

    tp_ok = False
    sl_ok = False
    last_error = ""

    methods = [
        {
            'name': 'take_profit_market + stop_market',
            'tp': {'type': 'take_profit_market', 'price': None, 'params': {'stopPrice': tp_price, 'reduceOnly': True}},
            'sl': {'type': 'stop_market',        'price': None, 'params': {'stopPrice': sl_price, 'reduceOnly': True}}
        },
        {
            'name': 'take_profit_market + stop_market (triggerPrice)',
            'tp': {'type': 'take_profit_market', 'price': None, 'params': {'triggerPrice': tp_price, 'reduceOnly': True}},
            'sl': {'type': 'stop_market',        'price': None, 'params': {'triggerPrice': sl_price, 'reduceOnly': True}}
        },
        {
            'name': 'limit TP + stop_market SL',
            'tp': {'type': 'limit',       'price': tp_price, 'params': {'reduceOnly': True}},
            'sl': {'type': 'stop_market', 'price': None,     'params': {'stopPrice': sl_price, 'reduceOnly': True}}
        },
        {
            'name': 'limit TP + limit SL',
            'tp': {'type': 'limit', 'price': tp_price, 'params': {'reduceOnly': True}},
            'sl': {'type': 'limit', 'price': sl_price, 'params': {'reduceOnly': True}}
        },
    ]

    for method in methods:
        try:
            print(f"Trying method: {method['name']}")
            if not tp_ok:
                exchange.create_order(
                    symbol,
                    method['tp']['type'],
                    close_side,
                    amount,
                    method['tp']['price'],
                    method['tp']['params']
                )
                tp_ok = True
                print(f"  ✅ TP ثبت شد با {method['name']}")
            if not sl_ok:
                exchange.create_order(
                    symbol,
                    method['sl']['type'],
                    close_side,
                    amount,
                    method['sl']['price'],
                    method['sl']['params']
                )
                sl_ok = True
                print(f"  ✅ SL ثبت شد با {method['name']}")
            if tp_ok and sl_ok:
                break
        except Exception as e:
            last_error = str(e)
            print(f"  ❌ {method['name']} failed: {e}")
            time.sleep(0.8)

    try:
        open_orders = exchange.fetch_open_orders(symbol)
        reduce_orders = [
            o for o in open_orders
            if o.get('reduceOnly') or o.get('info', {}).get('reduceOnly')
        ]
        print(f"تعداد سفارش‌های reduceOnly باز: {len(reduce_orders)}")
        if len(reduce_orders) < 2:
            tp_ok = False
            sl_ok = False
    except Exception as e:
        print(f"خطا در چک کردن سفارش‌های باز: {e}")

    if tp_ok and sl_ok:
        print(f"✅ TP/SL با موفقیت ثبت شد | TP: {tp_price} | SL: {sl_price}")
        return True, tp_price, sl_price
    else:
        print(f"❌ Failed to place both TP and SL. Last error: {last_error}")
        return False, tp_price, sl_price

def check_and_hard_stop(exchange, positions):
    """
    منطق جدید استاپ:
    - وقتی به ۱۲٪ ضرر رسید → وارد حالت صبر می‌شه (نمی‌بنده)
    - اگر برگشت به ۹٪ ضرر → با قیمت بهتر می‌بنده
    - اگر بدون برگشت به ۱۷٪ رسید → فوری اجباری می‌بنده
    """
    global positions_in_soft_stop

    current_symbols = set()

    for p in positions:
        try:
            symbol = p.get('symbol')
            side = p.get('side', '').lower()          # 'long' یا 'short'
            contracts = float(p.get('contracts', 0) or 0)
            entry_price = float(p.get('entryPrice', 0) or 0)
            
            if contracts <= 0 or entry_price <= 0:
                continue

            current_symbols.add(symbol)

            # قیمت فعلی
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker['last'])

            # محاسبه درصد حرکت قیمت
            if side in ['long', 'buy']:
                price_change_pct = (current_price - entry_price) / entry_price
            else:  # short
                price_change_pct = (entry_price - current_price) / entry_price

            # ---------- حالت نرمال ----------
            if symbol not in positions_in_soft_stop:
                if price_change_pct <= -SOFT_STOP_PCT:
                    # رسید به ۱۲٪ → فقط وارد حالت صبر شو، هنوز نبند
                    positions_in_soft_stop[symbol] = True
                    print(f"🟡 Soft Stop activated for {symbol} | change={price_change_pct*100:.3f}% → منتظر برگشت به ۹٪ یا فورس در ۱۷٪")
                    send_telegram(
                        f"🟡 <b>استاپ نرم ۱۲٪ فعال شد</b>\n"
                        f"نماد: <b>{symbol}</b>\n"
                        f"جهت: {side}\n"
                        f"ورود: {entry_price:.6f}\n"
                        f"قیمت فعلی: {current_price:.6f}\n"
                        f"ضرر فعلی: <b>{abs(price_change_pct)*100:.2f}%</b>\n"
                        f"⏳ صبر می‌کنم تا به ۹٪ برگرده (قیمت بهتر) یا اگر به ۱۷٪ رسید اجباری می‌بندم."
                    )
                continue

            # ---------- حالت soft stop (قبلاً به ۱۲٪ رسیده) ----------
            if price_change_pct <= -FORCE_STOP_PCT:
                # ۱۷٪ → فورس بستن
                print(f"🚨 FORCE Stop (17%) triggered for {symbol} | change={price_change_pct*100:.3f}%")
                close_side = 'sell' if side in ['long', 'buy'] else 'buy'
                amount = exchange.amount_to_precision(symbol, contracts)
                
                exchange.create_order(
                    symbol,
                    'market',
                    close_side,
                    amount,
                    None,
                    {'reduceOnly': True}
                )
                
                send_telegram(
                    f"🛑 <b>فورس استاپ ۱۷٪ فعال شد</b>\n"
                    f"نماد: <b>{symbol}</b>\n"
                    f"جهت: {side}\n"
                    f"ورود: {entry_price:.6f}\n"
                    f"قیمت فعلی: {current_price:.6f}\n"
                    f"ضرر: <b>{abs(price_change_pct)*100:.2f}%</b>\n"
                    f"پوزیشن به صورت اجباری بسته شد (بدون برگشت)."
                )
                if symbol in positions_in_soft_stop:
                    del positions_in_soft_stop[symbol]

            elif price_change_pct >= -RECOVERY_PCT:
                # برگشت به ۹٪ یا بهتر → بستن در قیمت بهتر
                print(f"✅ Recovery close (9%) for {symbol} | change={price_change_pct*100:.3f}%")
                close_side = 'sell' if side in ['long', 'buy'] else 'buy'
                amount = exchange.amount_to_precision(symbol, contracts)
                
                exchange.create_order(
                    symbol,
                    'market',
                    close_side,
                    amount,
                    None,
                    {'reduceOnly': True}
                )
                
                send_telegram(
                    f"✅ <b>بستن با قیمت بهتر (۹٪)</b>\n"
                    f"نماد: <b>{symbol}</b>\n"
                    f"جهت: {side}\n"
                    f"ورود: {entry_price:.6f}\n"
                    f"قیمت فعلی: {current_price:.6f}\n"
                    f"ضرر نهایی: 
