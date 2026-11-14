import os
import time
import threading
import requests
import pandas as pd
import numpy as np
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- تنظیمات تلگرام ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# --- تنظیمات مدیریت ریسک و استراتژی ---
LEVERAGE = 15
INITIAL_MARGIN = 0.2         # مارجین فرضی هر پوزیشن (دلار)
TAKE_PROFIT_PCT = 10.0       # ۱۰٪ سود با اهرم
MAX_LOSS_PCT = 22.0          # حد ضرر نهایی (فورس استاپ)
MAX_HOLD_MINUTES = 20        # حداکثر زمان باز ماندن پوزیشن

# حافظه پوزیشن‌های فعال شبیه‌سازی‌شده
active_positions = []
positions_in_soft_stop = {}

def send_telegram(message):
    """ارسال گزارش به تلگرام"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID":
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

def get_ohlcv(symbol, limit=50):
    """دریافت کندل‌های قیمتی از صرافی بایننس (بدون نیاز به کلید صرافی)"""
    clean_symbol = symbol.replace('/', '').replace(':USDT', 'USDT').replace('.P', '')
    if not clean_symbol.endswith('USDT'):
        clean_symbol += 'USDT'
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval=1m&limit={limit}"
        resp = requests.get(url, timeout=5).json()
        df = pd.DataFrame(resp, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'q_vol', 'trades', 't_buy_base', 't_buy_quote', 'ignore'])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        return df
    except Exception as e:
        print(f"Error fetching OHLCV for {symbol}: {e}")
        return None

# --- محاسبات تکنیکال دقیقاً مطابق پاین اسکریپت شما ---
def calculate_indicators(df):
    # EMA 9 و EMA 21
    df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
    
    # RSI 14
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    # Volume SMA 20
    df['vol_ma'] = df['volume'].rolling(window=20).mean()
    df['is_volume_high'] = df['volume'] > df['vol_ma']
    
    # ATR 14
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()
    
    return df

@app.route('/')
def home():
    return "Cyclo Strategy Python Bot is Running Successfully! 🚀"

def evaluate_market_symbols():
    """بررسی دوره‌ای بازار بر اساس قوانین دقیق پاین اسکریپت"""
    global active_positions
    
    # اگر از قبل پوزیشن بازی داریم، سیگنال جدید بررسی نمی‌شود تا پوزیشن بسته شود
    if len(active_positions) > 0:
        return

    # لیست ارزهای زیر نظر برای اسکن
    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ADAUSDT', 'XRPUSDT', 'DOGEUSDT']
    
    for symbol in symbols:
        df = get_ohlcv(symbol, limit=40)
        if df is None or len(df) < 25:
            continue
            
        df = calculate_indicators(df)
        
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        # شرایط ورود (Buy Condition) دقیقاً مشابه پاین اسکریپت
        # ta.crossover(emaFast, emaSlow) and isVolumeHigh and (rsiValue > 45)
        crossover = (prev['ema_fast'] <= prev['ema_slow']) and (curr['ema_fast'] > curr['ema_slow'])
        is_volume_high = curr['volume'] > curr['vol_ma']
        rsi_valid = curr['rsi'] > 45
        
        buy_condition = crossover and is_volume_high and rsi_valid
        
        if buy_condition:
            entry_price = curr['close']
            current_atr = curr['atr']
            
            # محاسبه حد ضرر ATR (stopLossLevel = close - (currentAtr * 2.0))
            stop_loss_level = entry_price - (current_atr * 2.0)
            
            open_paper_position(symbol, entry_price, stop_loss_level)
            break

def open_paper_position(symbol, entry_price, stop_loss_level):
    global active_positions
    
    new_pos = {
        'symbol': symbol,
        'entry_price': entry_price,
        'stop_loss_level': stop_loss_level,
        'open_time': time.time(),
        'margin': INITIAL_MARGIN,
        'max_pnl_seen': 0.0,
        'has_been_in_loss': False
    }
    active_positions.append(new_pos)
    
    send_telegram(
        f"🎯 <b>سیگنال خرید (استراتژی سیکلو) صادر شد</b>\n"
        f"نماد: <b>{symbol}</b> (اهرم {LEVERAGE}x)\n"
        f"قیمت ورود: {entry_price:.4f}\n"
        f"حد ضرر ATR: {stop_loss_level:.4f}"
    )

def monitor_positions():
    """مدیریت پوزیشن‌های باز، بررسی حد ضرر ATR، خروج‌ها و سود/زیان"""
    global active_positions, positions_in_soft_stop
    
    if not active_positions:
        return

    for pos in active_positions[:]:
        symbol = pos['symbol']
        entry_price = pos['entry_price']
        stop_loss_level = pos['stop_loss_level']
        elapsed_minutes = (time.time() - pos['open_time']) / 60

        df = get_ohlcv(symbol, limit=15)
        if df is None:
            continue
            
        df = calculate_indicators(df)
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        current_price = curr['close']
        rsi_value = curr['rsi']
        
        # محاسبه سود/زیان درصدی با اهرم ۱۵
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
        
        if pnl_pct > pos['max_pnl_seen']:
            pos['max_pnl_seen'] = pnl_pct
        if pnl_pct < 0:
            pos['has_been_in_loss'] = True

        # بررسی شرایط خروج (Sell Condition) مشابه پاین اسکریپت:
        # sellCondition = ta.crossunder(emaFast, emaSlow) or (close < stopLossLevel) or (rsiValue < 40)
        crossunder = (prev['ema_fast'] >= prev['ema_slow']) and (curr['ema_fast'] < curr['ema_slow'])
        hit_atr_sl = current_price < stop_loss_level
        rsi_exit = rsi_value < 40

        # ۱. خروج بر اساس شرایط تکنیکال استراتژی (تقاطع نزولی یا RSI < 40 یا برخورد به SL)
        if crossunder or hit_atr_sl or rsi_exit:
            reason = "برخورد به حد ضرر ATR 🛑" if hit_atr_sl else ("تقاطع نزولی EMA 📉" if crossunder else "افت RSI زیر ۴۰ 🔻")
            close_position(pos, current_price, reason)
            continue

        # ۲. حد سود ایده‌آل (۱۰٪) یا فورس استاپ نهایی (۲۲٪)
        if pnl_pct >= TAKE_PROFIT_PCT:
            close_position(pos, current_price, "هدف سود ۱۰ درصدی محقق شد 🎯")
            continue
            
        if pnl_pct <= -MAX_LOSS_PCT:
            close_position(pos, current_price, f"حد ضرر نهایی ({MAX_LOSS_PCT}%) 🛑")
            continue

        # ۳. بازار راکد و خروج با سود جزئی بعد از ۵ دقیقه
        if elapsed_minutes >= 5 and pnl_pct >= 0.01 and pos['max_pnl_seen'] < 2.0:
            close_position(pos, current_price, "بازار راکد - ذخیره سود جزئی 🟢")
            continue

        # ۴. سقف زمانی ۲۰ دقیقه‌ای
        if elapsed_minutes >= MAX_HOLD_MINUTES:
            close_position(pos, current_price, "پایان زمان مجاز ۲۰ دقیقه‌ای ⏰")
            continue

def close_position(pos, exit_price, reason):
    global active_positions
    if pos in active_positions:
        active_positions.remove(pos)
        
    entry_price = pos['entry_price']
    duration = round((time.time() - pos['open_time']) / 60, 1)
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE
    
    emoji = "✅" if pnl_pct >= 0 else "🔻"
    send_telegram(
        f"{emoji} <b>پوزیشن بسته شد</b>\n"
        f"دلیل: <b>{reason}</b>\n"
        f"نماد: <b>{pos['symbol']}</b>\n"
        f"ورود: {entry_price:.4f} ➔ خروج: {exit_price:.4f}\n"
        f"مدت معامله: {duration} دقیقه\n"
        f"نتیجه نهایی (اهرم {LEVERAGE}x): <b>{pnl_pct:+.2f}%</b>"
    )

def background_loop():
    while True:
        try:
            evaluate_market_symbols()
            monitor_positions()
        except Exception as e:
            print(f"Background loop error: {e}")
        time.sleep(15)

if __name__ == '__main__':
    t = threading.Thread(target=background_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
