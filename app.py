import os
import time
import threading
import requests
import ccxt
import pandas as pd
from flask import Flask

app = Flask(__name__)

# روتی برای پاسخ به پینگ‌های Render و جلوگیری از خطای پورت
@app.route('/')
def home():
    return "Toobit Scalp Bot with Multi-TF Filter is Running Successfully! 🚀"

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

# استاپ جدید
SOFT_STOP_PCT   = 0.12 / LEVERAGE   # ۱۲٪ → شروع مانیتورینگ
RECOVERY_PCT    = 0.09 / LEVERAGE   # ۹٪ → بستن در قیمت بهتر
FORCE_STOP_PCT  = 0.17 / LEVERAGE   # ۱۷٪ → بستن اجباری فوری

# متغیرهای کنترل
last_traded_symbol = None
last_trade_direction = None
last_trade_time = 0
MIN_SECONDS_BETWEEN_TRADES = 45

# وضعیت پوزیشن‌هایی که به ۱۲٪ رسیدن
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
        try:
            exchange.set_leverage(LEVERAGE, symbol, params={'marginMode': 'isolated'})
            print(f"✅ مود ایزوله از طریق set_leverage برای {symbol} تنظیم شد")
            return True
        except Exception as e2:
            print(f"Fallback set_leverage also failed: {e2}")
            return False

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    rsi = 100 - (100 / (1 + rs))
    return rsi

def calculate_atr(df, period=14):
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return atr

def calculate_roc_acceleration(df, period=3):
    """محاسبه نرخ تغییرات قیمت (ROC) و شتاب شتابان آن"""
    roc = df['close'].pct_change(periods=period) * 100
    roc_acceleration = roc.diff()
    return roc, roc_acceleration

def fetch_higher_timeframes(exchange, symbol):
    """تحلیل روند تایم‌فریم‌های بالاتر (1H, 4H, 1D)"""
    try:
        # دریافت کندل‌های 1H, 4H, 1D
        ohlcv_1h = exchange.fetch_ohlcv(symbol, timeframe='1h', limit=50)
        ohlcv_4h = exchange.fetch_ohlcv(symbol, timeframe='4h', limit=50)
        ohlcv_1d = exchange.fetch_ohlcv(symbol, timeframe='1d', limit=50)

        if not ohlcv_1h or not ohlcv_4h or not ohlcv_1d:
            return 'NEUTRAL'

        df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_4h = pd.DataFrame(ohlcv_4h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_1d = pd.DataFrame(ohlcv_1d, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

        # محاسبه EMA و RSI برای ۴ ساعته و روزانه
        df_4h['ema20'] = df_4h['close'].ewm(span=20, adjust=False).mean()
        df_4h['ema50'] = df_4h['close'].ewm(span=50, adjust=False).mean()
        
        df_1d['ema20'] = df_1d['close'].ewm(span=20, adjust=False).mean()
        df_1d['close_val'] = df_1d['close']

        # سیگنال‌های روند
        trend_4h_bullish = df_4h['ema20'].iloc[-1] > df_4h['ema50'].iloc[-1]
        trend_1d_bullish = df_1d['close_val'].iloc[-1] > df_1d['ema20'].iloc[-1]
        
        trend_4h_bearish = df_4h['ema20'].iloc[-1] < df_4h['ema50'].iloc[-1]
        trend_1d_bearish = df_1d['close_val'].iloc[-1] < df_1d['ema20'].iloc[-1]

        # تصمیم‌گیری روند کلان بازار
        if trend_4h_bullish and trend_1d_bullish:
            return 'STRONG_BULLISH'
        elif trend_4h_bearish and trend_1d_bearish:
            return 'STRONG_BEARISH'
        elif trend_4h_bullish:
            return 'BULLISH'
        elif trend_4h_bearish:
            return 'BEARISH'
        else:
            return 'NEUTRAL'

    except Exception as e:
        print(f"Higher TF Fetch Error for {symbol}: {e}")
        return 'NEUTRAL'

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
            if not tp_ok:
                exchange.create_order(symbol, method['tp']['type'], close_side, amount, method['tp']['price'], method['tp']['params'])
                tp_ok = True
            if not sl_ok:
                exchange.create_order(symbol, method['sl']['type'], close_side, amount, method['sl']['price'], method['sl']['params'])
                sl_ok = True
            if tp_ok and sl_ok:
                break
        except Exception as e:
            last_error = str(e)
            time.sleep(0.8)

    return (tp_ok and sl_ok), tp_price, sl_price

def check_and_hard_stop(exchange, positions):
    global positions_in_soft_stop
    current_symbols = set()

    for p in positions:
        try:
            symbol = p.get('symbol')
            side = p.get('side', '').lower()
            contracts = float(p.get('contracts', 0) or 0)
            entry_price = float(p.get('entryPrice', 0) or 0)
            
            if contracts <= 0 or entry_price <= 0:
                continue

            current_symbols.add(symbol)
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker['last'])

            if side in ['long', 'buy']:
                price_change_pct = (current_price - entry_price) / entry_price
            else:
                price_change_pct = (entry_price - current_price) / entry_price

            if symbol not in positions_in_soft_stop:
                if price_change_pct <= -SOFT_STOP_PCT:
                    positions_in_soft_stop[symbol] = True
                    send_telegram(
                        f"🟡 <b>استاپ نرم ۱۲٪ فعال شد</b>\n"
                        f"نماد: <b>{symbol}</b> | جهت: {side}\n"
                        f"ضرر فعلی: <b>{abs(price_change_pct)*100:.2f}%</b>"
                    )
                continue

            if price_change_pct <= -FORCE_STOP_PCT:
                close_side = 'sell' if side in ['long', 'buy'] else 'buy'
                amount = exchange.amount_to_precision(symbol, contracts)
                exchange.create_order(symbol, 'market', close_side, amount, None, {'reduceOnly': True})
                send_telegram(f"🛑 <b>فورس استاپ ۱۷٪ فعال شد</b>\nنماد: <b>{symbol}</b>")
                positions_in_soft_stop.pop(symbol, None)

            elif price_change_pct >= -RECOVERY_PCT:
                close_side = 'sell' if side in ['long', 'buy'] else 'buy'
                amount = exchange.amount_to_precision(symbol, contracts)
                exchange.create_order(symbol, 'market', close_side, amount, None, {'reduceOnly': True})
                send_telegram(f"✅ <b>بستن با قیمت بهتر (۹٪)</b>\nنماد: <b>{symbol}</b>")
                positions_in_soft_stop.pop(symbol, None)

        except Exception as e:
            print(f"Error in hard stop check: {e}")

    for sym in list(positions_in_soft_stop.keys()):
        if sym not in current_symbols:
            del positions_in_soft_stop[sym]

def run_bot_loop():
    global last_traded_symbol, last_trade_direction, last_trade_time, positions_in_soft_stop
    time.sleep(10)
    send_telegram(
        "🚀 <b>ربات اسکالپ با تحلیل تایم‌فریم بالادستی فعال شد</b>\n"
        "• فیلتر روند کلان: تحلیل کندل‌های 1D و 4H\n"
        "• فیلتر شتاب: ROC Acceleration روی کندل ۵ دقیقه"
    )
    
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

            if current_active_count > 0:
                check_and_hard_stop(exchange, active_positions)
                
                if time.time() - last_status_report > 120:
                    msg = "📊 <b>وضعیت فعلی پوزیشن‌ها:</b>\n"
                    for p in active_positions:
                        symbol = p.get('symbol', 'N/A')
                        side = p.get('side', 'N/A')
                        entry = float(p.get('entryPrice', 0) or 0)
                        unrealized = float(p.get('unrealizedPnl', 0) or 0)
                        msg += f"• {symbol} | {side} | ورود: {entry:.4f} | PnL: <b>{unrealized:.4f}</b>\n"
                    send_telegram(msg)
                    last_status_report = time.time()

                previous_active_count = current_active_count
                time.sleep(8)
                continue

            if previous_active_count > 0 and current_active_count == 0:
                send_telegram("✅ همه پوزیشن‌ها بسته شدند. اسکن کل بازار...")
                previous_active_count = 0
                positions_in_soft_stop.clear()

            if time.time() - last_trade_time < MIN_SECONDS_BETWEEN_TRADES:
                time.sleep(10)
                continue

            tickers = exchange.fetch_tickers()
            candidates = []
            
            for symbol, data in tickers.items():
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
                
            for symbol, change, vol in candidates[:15]:
                try:
                    df_5m, df_10m = fetch_and_resample_candles(exchange, symbol)
                    if df_5m is None or len(df_5m) < 20 or df_10m is None or len(df_10m) < 10:
                        continue

                    # ۱. فیلتر نوسانات ATR
                    atr_series = calculate_atr(df_5m, period=14)
                    current_atr = atr_series.iloc[-2]
                    avg_atr = atr_series.iloc[-15:-2].mean()

                    if pd.isna(current_atr) or pd.isna(avg_atr) or current_atr < avg_atr * 1.15:
                        continue

                    # ۲. فیلتر مومنتوم شتابی (ROC Acceleration)
                    df_5m['roc'], df_5m['roc_accel'] = calculate_roc_acceleration(df_5m, period=3)
                    current_accel = df_5m['roc_accel'].iloc[-2]
                    avg_accel = df_5m['roc_accel'].abs().iloc[-15:-2].mean()

                    if pd.isna(current_accel) or pd.isna(avg_accel) or abs(current_accel) < avg_accel * 1.3:
                        continue

                    # ۳. تحلیل روند کلان تایم‌فریم‌های بالادستی (1D, 4H, 1H)
                    macro_trend = fetch_higher_timeframes(exchange, symbol)

                    # محاسبه اندیکاتورهای ۵ دقیقه‌ای
                    c_close_5m = df_5m['close'].iloc[-2]
                    df_5m['ema5'] = df_5m['close'].ewm(span=5, adjust=False).mean()
                    df_5m['ema13'] = df_5m['close'].ewm(span=13, adjust=False).mean()
                    df_5m['rsi'] = calculate_rsi(df_5m['close'], period=14)
                    
                    c_rsi_5m = df_5m['rsi'].iloc[-2]
                    ema5_5m = df_5m['ema5'].iloc[-2]
                    ema13_5m = df_5m['ema13'].iloc[-2]

                    side = None
                    
                    # استراتژی ورود با تاییدیه تایم‌فریم بالادست
                    if ema5_5m > ema13_5m and df_5m['ema5'].iloc[-3] <= df_5m['ema13'].iloc[-3]:
                        if macro_trend in ['BULLISH', 'STRONG_BULLISH']:
                            side = 'buy'
                    elif ema5_5m < ema13_5m and df_5m['ema5'].iloc[-3] >= df_5m['ema13'].iloc[-3]:
                        if macro_trend in ['BEARISH', 'STRONG_BEARISH']:
                            side = 'sell'
                    elif c_rsi_5m < 28 and current_accel > 0:
                        if macro_trend != 'STRONG_BEARISH':  # فقط وقتی کلان شدیداً نزولی نیست Long بگیر
                            side = 'buy'
                    elif c_rsi_5m > 72 and current_accel < 0:
                        if macro_trend != 'STRONG_BULLISH':  # فقط وقتی کلان شدیداً صعودی نیست Short بگیر
                            side = 'sell'

                    if not side:
                        continue
                        
                    if symbol == last_traded_symbol and side == last_trade_direction:
                        continue
                        
                    set_isolated_mode(exchange, symbol)
                    try:
                        exchange.set_leverage(LEVERAGE, symbol)
                    except Exception as lev_err:
                        print(f"set_leverage warning: {lev_err}")
                        
                    market = exchange.market(symbol)
                    min_cost = market.get('limits', {}).get('cost', {}).get('min', 1.0) or 1.0
                    effective_cost = max(MAX_MARGIN * LEVERAGE, min_cost)
                    
                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    amount = effective_cost / price
                    amount = exchange.amount_to_precision(symbol, amount)
                    
                    if float(amount) <= 0:
                        continue
                        
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, amount)
                    else:
                        order = exchange.create_market_sell_order(symbol, amount)

                    entry_price = float(order.get('average') or order.get('price') or price)

                    tp_sl_success, tp_price, sl_price = place_tp_sl_orders(
                        exchange, symbol, side, amount, entry_price
                    )

                    send_telegram(
                        f"🎯 <b>پوزیشن جدید باز شد</b>\n"
                        f"نماد: <b>{symbol}</b> | جهت: <b>{side.upper()}</b>\n"
                        f"روند کلان (4H/1D): <b>{macro_trend}</b>\n"
                        f"ورود: {entry_price:.6f} | TP: {tp_price:.6f} | SL: {sl_price:.6f}"
                    )
                    
                    last_traded_symbol = symbol
                    last_trade_direction = side
                    last_trade_time = time.time()
                    previous_active_count = 1
                    break
                    
                except Exception as ex:
                    print(f"Error processing {symbol}: {ex}")
                    continue

        except Exception as loop_err:
            print(f"Main loop error: {loop_err}")
            time.sleep(15)

if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
