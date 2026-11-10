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
TARGET_MARGIN = 0.3  
TAKE_PROFIT_PCT = 0.008  # 0.8% حرکت قیمت
STOP_LOSS_PCT = 0.004    # 0.4% حرکت قیمت

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

def fetch_and_resample_candles(exchange, symbol):
    """دریافت کندل‌های ۵ دقیقه و ساخت کندل‌های ۱۰ دقیقه"""
    try:
        ohlcv_5m = exchange.fetch_ohlcv(symbol, timeframe='5m', limit=60)
        if len(ohlcv_5m) < 30:
            return None, None
            
        df_5m = pd.DataFrame(ohlcv_5m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df_5m['timestamp'] = pd.to_datetime(df_5m['timestamp'], unit='ms')
        df_5m.set_index('timestamp', inplace=True)
        
        # ساخت دیتافریم ۱۰ دقیقه از روی ۵ دقیقه
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

def run_bot_loop():
    time.sleep(10)  
    send_telegram("🚀 <b>ربات اسکالپ (کندل‌های ۵ و ۱۰ دقیقه‌ای + استراتژی انعطاف‌پذیر) استارت شد!</b>")
    
    exchange = get_toobit_exchange()
    try:
        exchange.load_markets()
        balance = exchange.fetch_balance()
        usdt_free = balance['free'].get('USDT', 0)
        send_telegram(f"✅ اتصال برقرار شد.\n💰 موجودی آزاد: <b>{usdt_free} USDT</b>")
    except Exception as e:
        send_telegram(f"❌ خطا در اتصال: {e}")
        return
        
    last_traded_symbol = None
    
    while True:
        try:
            positions = exchange.fetch_positions()
            active_positions = [p for p in positions if float(p.get('contracts', 0) or 0) > 0]
            
            if len(active_positions) > 0:
                time.sleep(15)
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
                time.sleep(20)
                continue
                
            traded_successfully = False
            
            for symbol, change, vol in candidates[:15]:
                try:
                    df_5m, df_10m = fetch_and_resample_candles(exchange, symbol)
                    if df_5m is None or len(df_5m) < 20 or df_10m is None or len(df_10m) < 10:
                        continue
                        
                    # --- تحلیل تایم‌فریم ۵ دقیقه ---
                    df_5m['ema5'] = df_5m['close'].ewm(span=5, adjust=False).mean()
                    df_5m['ema13'] = df_5m['close'].ewm(span=13, adjust=False).mean()
                    df_5m['rsi'] = calculate_rsi(df_5m['close'], period=14)
                    
                    c_close_5m = df_5m['close'].iloc[-2]
                    c_rsi_5m = df_5m['rsi'].iloc[-2]
                    ema5_5m = df_5m['ema5'].iloc[-2]
                    ema13_5m = df_5m['ema13'].iloc[-2]
                    
                    long_fib_5m, short_fib_5m, _, _ = calculate_fibonacci_levels(df_5m)
                    
                    # --- تحلیل تایم‌فریم ۱۰ دقیقه (بررسی شدوها و روند) ---
                    c_10m = df_10m.iloc[-2]
                    o_10m, h_10m, l_10m, cls_10m = c_10m['open'], c_10m['high'], c_10m['low'], c_10m['close']
                    range_10m = h_10m - l_10m
                    
                    upper_wick_10m = (h_10m - max(o_10m, cls_10m)) / range_10m if range_10m > 0 else 0
                    lower_wick_10m = (min(o_10m, cls_10m) - l_10m) / range_10m if range_10m > 0 else 0
                    
                    side = None
                    
                    # ==========================================
                    # سیستم OR (هرکدام از شرط‌های زیر برقرار شد)
                    # ==========================================
                    
                    # شرط ۱: کراس صعودی EMA در ۵ دقیقه
                    if ema5_5m > ema13_5m and df_5m['ema5'].iloc[-3] <= df_5m['ema13'].iloc[-3]:
                        side = 'buy'
                    # شرط ۲: کراس نزولی EMA در ۵ دقیقه
                    elif ema5_5m < ema13_5m and df_5m['ema5'].iloc[-3] >= df_5m['ema13'].iloc[-3]:
                        side = 'sell'
                        
                    # شرط ۳: برخورد قیمت با محدوده طلایی فیبوناچی در ۵ دقیقه
                    elif long_fib_5m and long_fib_5m['0.618'] <= c_close_5m <= long_fib_5m['0.382']:
                        side = 'buy'
                    elif short_fib_5m and short_fib_5m['0.382'] <= c_close_5m <= short_fib_5m['0.618']:
                        side = 'sell'
                        
                    # شرط ۴: شدوی بلند صعودی یا نزولی در کندل ۱۰ دقیقه (ریجکت قیمت)
                    elif lower_wick_10m >= 0.35:
                        side = 'buy'
                    elif upper_wick_10m >= 0.35:
                        side = 'sell'
                        
                    # شرط ۵: خروج RSI از محدوده‌های اشباع
                    elif c_rsi_5m < 30:
                        side = 'buy'
                    elif c_rsi_5m > 70:
                        side = 'sell'

                    if not side:
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
                        
                    # ثبت سفارش خرید یا فروش
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, amount)
                        entry_price = float(order.get('average', price))
                        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
                        sl_price = entry_price * (1 - STOP_LOSS_PCT)
                        
                        try:
                            exchange.create_order(symbol, 'take_profit_market', 'sell', amount, None, {'stopPrice': tp_price, 'reduceOnly': True})
                            exchange.create_order(symbol, 'stop_market', 'sell', amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
                        except Exception as ex:
                            print(f"TP/SL Order Error: {ex}")
                    else:
                        order = exchange.create_market_sell_order(symbol, amount)
                        entry_price = float(order.get('average', price))
                        tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
                        sl_price = entry_price * (1 + STOP_LOSS_PCT)
                        
                        try:
                            exchange.create_order(symbol, 'take_profit_market', 'buy', amount, None, {'stopPrice': tp_price, 'reduceOnly': True})
                            exchange.create_order(symbol, 'stop_market', 'buy', amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
                        except Exception as ex:
                            print(f"TP/SL Order Error: {ex}")
                        
                    send_telegram(
                        f"🎯 <b>پوزیشن جدید (مبتنی بر شرایط انعطاف‌پذیر 5m و 10m)!</b>\n"
                        f" نماد: <b>{symbol}</b>\n"
                        f" جهت: <b>{'خرید (Long)' if side == 'buy' else 'فروش (Short)'}</b>\n"
                        f" قیمت ورود: <b>{entry_price}</b>\n"
                        f" حد سود: {tp_price:.4f} | حد ضرر: {sl_price:.4f}"
                    )
                    
                    last_traded_symbol = symbol
                    traded_successfully = True
                    break
                    
                except Exception as ex:
                    print(f"Error processing {symbol}: {ex}")
                    continue
                    
            if not traded_successfully:
                time.sleep(30)
                
        except Exception as e:
            print(f"Loop Error: {e}")
            time.sleep(15)

@app.route('/')
def home():
    return "Flexible Multi-Timeframe Scalp Bot is running!"

if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
