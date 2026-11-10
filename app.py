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

# تنظیمات اسکلپ ۱ دقیقه‌ای
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

def calculate_fibonacci_levels(df):
    """محاسبه سطوح فیبوناچی اصلاحی با خروجی پایدار چهار مقداره"""
    high = df['high'].max()
    low = df['low'].min()
    diff = high - low
    
    if diff == 0:
        return None, None, high, low
        
    # سطوح اصلاحی استاندارد از سقف به کف (برای لانگ / پولبک صعودی)
    long_levels = {
        '0.382': high - 0.382 * diff,
        '0.5': high - 0.5 * diff,
        '0.618': high - 0.618 * diff
    }
    
    # سطوح اصلاحی از کف به سقف (برای شورت / پولبک نزولی)
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

def analyze_higher_tf_shadows(exchange, symbol):
    """بررسی شدوهای کندل ۱۵ دقیقه برای تایید ریجکت قیمت"""
    try:
        ohlcv_15m = exchange.fetch_ohlcv(symbol, timeframe='15m', limit=5)
        if len(ohlcv_15m) < 3:
            return None
        
        candle = ohlcv_15m[-2]
        o, h, l, c = candle[1], candle[2], candle[3], candle[4]
        
        total_range = h - l
        if total_range == 0:
            return None
            
        upper_wick = h - max(o, c)
        lower_wick = min(o, c) - l
        
        if lower_wick >= (total_range * 0.35):
            return 'buy'
        elif upper_wick >= (total_range * 0.35):
            return 'sell'
            
    except Exception as e:
        print(f"Shadow Analysis Error for {symbol}: {e}")
        
    return None

def run_bot_loop():
    time.sleep(10)  
    send_telegram("🚀 <b>ربات اسکالپ توبیت (نسخه دقیق با کندل‌های بسته شده) استارت شد!</b>")
    
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
                    ohlcv = exchange.fetch_ohlcv(symbol, timeframe='1m', limit=40)
                    if len(ohlcv) < 30:
                        continue
                        
                    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
                    
                    # محاسبه اندیکاتورها
                    df['ema5'] = df['close'].ewm(span=5, adjust=False).mean()
                    df['ema13'] = df['close'].ewm(span=13, adjust=False).mean()
                    df['rsi'] = calculate_rsi(df['close'], period=14)
                    
                    # بررسی روی کندل‌های قطعی و بسته‌شده (-2) برای جلوگیری از سیگنال‌های فیک لحظه‌ای
                    prev_ema5 = df['ema5'].iloc[-3]
                    prev_ema13 = df['ema13'].iloc[-3]
                    curr_ema5 = df['ema5'].iloc[-2]
                    curr_ema13 = df['ema13'].iloc[-2]
                    curr_close = df['close'].iloc[-2]
                    curr_rsi = df['rsi'].iloc[-2]
                    
                    long_fib, short_fib, _, _ = calculate_fibonacci_levels(df)
                    if long_fib is None:
                        continue
                    
                    side = None
                    
                    # شرایط خرید (Long): کراس صعودی در کندل بسته شده + قیمت در فیبو + RSI صعودی
                    if prev_ema5 <= prev_ema13 and curr_ema5 > curr_ema13:
                        if long_fib['0.618'] <= curr_close <= long_fib['0.382']:
                            if curr_rsi > 48:
                                side = 'buy'
                                
                    # شرایط فروش (Short): کراس نزولی در کندل بسته شده + قیمت در فیبو + RSI نزولی
                    elif prev_ema5 >= prev_ema13 and curr_ema5 < curr_ema13:
                        if short_fib['0.382'] <= curr_close <= short_fib['0.618']:
                            if curr_rsi < 52:
                                side = 'sell'
                            
                    # تاییدیه نهایی با شدوهای ۱۵ دقیقه
                    if side:
                        htf_signal = analyze_higher_tf_shadows(exchange, symbol)
                        if htf_signal and htf_signal != side:
                            side = None

                    if not side:
                        continue
                        
                    try:
                        exchange.set_leverage(LEVERAGE, symbol)
                    except:
                        pass
                        
                    market = exchange.market(symbol)
                    min_cost = market.get('limits', {}).get('cost', {}).get('min', 1.0) or 1.0
                    effective_cost = max(TARGET_MARGIN * LEVERAGE, min_cost)
                    
                    # قیمت لحظه‌ای برای ورود دقیق بازار
                    ticker = exchange.fetch_ticker(symbol)
                    price = ticker['last']
                    amount = effective_cost / price
                    amount = exchange.amount_to_precision(symbol, amount)
                    
                    if float(amount) <= 0:
                        continue
                        
                    # ثبت سفارش بازار
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, amount)
                        entry_price = float(order.get('average', price))
                        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
                        sl_price = entry_price * (1 - STOP_LOSS_PCT)
                        
                        try:
                            exchange.create_order(symbol, 'take_profit_market', 'sell', amount, None, {'stopPrice': tp_price, 'reduceOnly': True})
                            exchange.create_order(symbol, 'stop_market', 'sell', amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
                        except Exception as ex:
                            print(f"Exchange TP/SL Order Error: {ex}")
                            
                    else:
                        order = exchange.create_market_sell_order(symbol, amount)
                        entry_price = float(order.get('average', price))
                        tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
                        sl_price = entry_price * (1 + STOP_LOSS_PCT)
                        
                        try:
                            exchange.create_order(symbol, 'take_profit_market', 'buy', amount, None, {'stopPrice': tp_price, 'reduceOnly': True})
                            exchange.create_order(symbol, 'stop_market', 'buy', amount, None, {'stopPrice': sl_price, 'reduceOnly': True})
                        except Exception as ex:
                            print(f"Exchange TP/SL Order Error: {ex}")
                        
                    send_telegram(
                        f"🎯 <b>اسکلپ موفق (بدون سیگنال فیک)!</b>\n"
                        f" نماد: <b>{symbol}</b>\n"
                        f" جهت: <b>{'خرید (Long)' if side == 'buy' else 'فروش (Short)'}</b>\n"
                        f" قیمت ورود: <b>{entry_price}</b>\n"
                        f" حد سود ثبت‌شده: {tp_price:.4f}\n"
                        f" حد ضرر ثبت‌شده: {sl_price:.4f}\n"
                        f" RSI کندل بسته شده: {curr_rsi:.1f}"
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
    return "Stable Scalp Bot with Closed Candles is running!"

if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
