import os
import time
import datetime
import threading
import requests
import pandas as pd
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

# متغیرهای آماری برای گزارش‌گیری ساعتی و شبانه
active_positions = []
hourly_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}
daily_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}
last_hourly_report_hour = datetime.datetime.now().hour
last_daily_report_day = datetime.datetime.now().day

def send_telegram(message):
    """ارسال پیام به تلگرام"""
    if not TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID == "YOUR_CHAT_ID":
        print(f"[Telegram Mock] {message}")
        return
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
        requests.post(url, data=payload, timeout=8)
    except Exception as e:
        print(f"Telegram Error: {e}")

def get_ohlcv(symbol, limit=50):
    """دریافت کندل‌های ۵ دقیقه‌ای از بایننس"""
    clean_symbol = symbol.replace('/', '').replace(':USDT', 'USDT').replace('.P', '')
    if not clean_symbol.endswith('USDT'):
        clean_symbol += 'USDT'
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={clean_symbol}&interval=5m&limit={limit}"
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

def calculate_indicators(df):
    """محاسبه اندیکاتورهای استراتژی سیکلو (EMA, RSI, حجم، ATR)"""
    df['ema_fast'] = df['close'].ewm(span=9, adjust=False).mean()
    df['ema_slow'] = df['close'].ewm(span=21, adjust=False).mean()
    
    delta = df['close'].diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
    rs = gain / loss
    df['rsi'] = 100 - (100 / (1 + rs))
    
    df['vol_ma'] = df['volume'].rolling(window=20).mean()
    df['is_volume_high'] = df['volume'] > df['vol_ma']
    
    high_low = df['high'] - df['low']
    high_close = (df['high'] - df['close'].shift()).abs()
    low_close = (df['low'] - df['low'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()
    
    return df

@app.route('/')
def home():
    return "Cyclo Trading Bot (Long & Short - TACUSDT 5m) is Running! 🚀"

def evaluate_market_symbols():
    """اسکن ارز TACUSDT برای تشخیص سیگنال‌های لانگ و شورت"""
    global active_positions
    if len(active_positions) > 0:
        return

    df = get_ohlcv('TACUSDT', limit=40)
    if df is None or len(df) < 25:
        return
        
    df = calculate_indicators(df)
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    
    # شرایط خرید (LONG): تقاطع رو به بالا EMA + حجم بالا + RSI > 45
    long_condition = (prev['ema_fast'] <= prev['ema_slow']) and (curr['ema_fast'] > curr['ema_slow']) and (curr['volume'] > curr['vol_ma']) and (curr['rsi'] > 45)
    
    # شرایط فروش/شورت (SHORT): تقاطع رو به پایین EMA + حجم بالا + RSI < 55
    short_condition = (prev['ema_fast'] >= prev['ema_slow']) and (curr['ema_fast'] < curr['ema_slow']) and (curr['volume'] > curr['vol_ma']) and (curr['rsi'] < 55)
    
    entry_price = curr['close']
    
    if long_condition:
        stop_loss_level = entry_price - (curr['atr'] * 2.0)
        open_paper_position('TACUSDT', 'LONG', entry_price, stop_loss_level)
    elif short_condition:
        stop_loss_level = entry_price + (curr['atr'] * 2.0)
        open_paper_position('TACUSDT', 'SHORT', entry_price, stop_loss_level)

def open_paper_position(symbol, side, entry_price, stop_loss_level):
    global active_positions
    
    new_pos = {
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'stop_loss_level': stop_loss_level,
        'open_time': time.time(),
        'margin': INITIAL_MARGIN,
        'max_pnl_seen': 0.0,
        'has_been_in_loss': False
    }
    active_positions.append(new_pos)
    
    emoji = "🚀" if side == 'LONG' else "📉"
    send_telegram(
        f"{emoji} <b>پوزیشن جدید باز شد ({side} - TACUSDT ۵m)</b>\n"
        f"پلتفرم: <b>شبیه‌ساز (Paper Trading)</b>\n"
        f"قیمت ورود: <b>{entry_price:.4f}</b> | اهرم: <b>{LEVERAGE}x</b>\n"
        f"حد ضرر ATR: {stop_loss_level:.4f}"
    )

def monitor_positions():
    """مدیریت پوزیشن‌های باز (لانگ و شورت) و بررسی شرایط خروج، راکد بودن و سود 0.01"""
    global active_positions, hourly_stats, daily_stats
    if not active_positions:
        return

    for pos in active_positions[:]:
        symbol = pos['symbol']
        side = pos['side']
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
        
        # محاسبه سود/زیان با توجه به جهت پوزیشن (لانگ یا شورت)
        if side == 'LONG':
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
            hit_sl = current_price < stop_loss_level
            technical_exit = (prev['ema_fast'] >= prev['ema_slow']) and (curr['ema_fast'] < curr['ema_slow']) or (rsi_value < 40)
        else: # SHORT
            pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
            hit_sl = current_price > stop_loss_level
            technical_exit = (prev['ema_fast'] <= prev['ema_slow']) and (curr['ema_fast'] > curr['ema_slow']) or (rsi_value > 60)
        
        if pnl_pct > pos['max_pnl_seen']:
            pos['max_pnl_seen'] = pnl_pct
        if pnl_pct < 0:
            pos['has_been_in_loss'] = True

        # ۱. حد ضرر ATR یا خروج تکنیکال
        if hit_sl or technical_exit:
            reason = "برخورد به حد ضرر ATR 🛑" if hit_sl else "سیگنال خروج تکنیکال 📉"
            close_position(pos, current_price, reason)
            continue

        # ۲. تارگت سود ۱۰٪ یا فورس استاپ ۲۲٪
        if pnl_pct >= TAKE_PROFIT_PCT:
            close_position(pos, current_price, "هدف سود ۱۰ درصدی محقق شد 🎯")
            continue
            
        if pnl_pct <= -MAX_LOSS_PCT:
            close_position(pos, current_price, f"حد ضرر نهایی ({MAX_LOSS_PCT}%) 🛑")
            continue

        # ۳. بازار راکد و ذخیره سود جزئی (مثلاً ۰.۰۱٪ به بالا پس از چند دقیقه)
        if elapsed_minutes >= 3 and pnl_pct >= 0.01 and pos['max_pnl_seen'] < 2.0:
            close_position(pos, current_price, "بازار راکد - ذخیره سود جزئی (۰.۰۱٪+) 🟢")
            continue

        # ۴. سقف زمانی ۲۰ دقیقه
        if elapsed_minutes >= MAX_HOLD_MINUTES:
            close_position(pos, current_price, "پایان زمان مجاز ۲۰ دقیقه‌ای ⏰")
            continue

def close_position(pos, exit_price, reason):
    global active_positions, hourly_stats, daily_stats
    if pos in active_positions:
        active_positions.remove(pos)
        
    side = pos['side']
    entry_price = pos['entry_price']
    duration = round((time.time() - pos['open_time']) / 60, 1)
    
    if side == 'LONG':
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100 * LEVERAGE
        
    pnl_usd = pos['margin'] * (pnl_pct / 100)

    hourly_stats['total_trades'] += 1
    daily_stats['total_trades'] += 1
    hourly_stats['net_pnl_usd'] += pnl_usd
    daily_stats['net_pnl_usd'] += pnl_usd
    
    if pnl_pct >= 0:
        hourly_stats['wins'] += 1
        daily_stats['wins'] += 1
        emoji = "✅"
    else:
        hourly_stats['losses'] += 1
        daily_stats['losses'] += 1
        emoji = "🔻"

    send_telegram(
        f"{emoji} <b>گزارش بستن پوزیشن ({side})</b>\n"
        f"نماد: <b>{pos['symbol']}</b> | دلیل: <b>{reason}</b>\n"
        f"ورود: {entry_price:.4f} ➔ خروج: {exit_price:.4f}\n"
        f"مدت معامله: {duration} دقیقه\n"
        f"سود/زیان درصدی: <b>{pnl_pct:+.2f}%</b> (دلاری: ${pnl_usd:+.2f})"
    )

def check_time_based_reports():
    global hourly_stats, daily_stats, last_hourly_report_hour, last_hourly_report_day
    now = datetime.datetime.now()
    current_hour = now.hour
    current_day = now.day

    if current_hour != last_hourly_report_hour:
        last_hourly_report_hour = current_hour
        send_telegram(
            f"📊 <b>گزارش ساعتی TACUSDT (ساعت {current_hour}:00)</b>\n"
            f"• کل معاملات: <b>{hourly_stats['total_trades']}</b> (برد: {hourly_stats['wins']} | باخت: {hourly_stats['losses']})\n"
            f"• سود/زیان خالص: <b>${hourly_stats['net_pnl_usd']:+.2f}</b>"
        )
        hourly_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}

    if current_day != last_hourly_report_day and current_hour == 0:
        last_hourly_report_day = current_day
        send_telegram(
            f"🌙 <b>گزارش پایانی روزانه TACUSDT</b>\n"
            f"• کل معاملات امروز: <b>{daily_stats['total_trades']}</b>\n"
            f"• 💰 <b>سود/زیان کل: ${daily_stats['net_pnl_usd']:+.2f}</b>"
        )
        daily_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}

def background_loop():
    send_telegram("🟢 <b>ربات دوطرفه TACUSDT (لانگ و شورت) روی تایم‌فریم ۵m استارت شد!</b>")
    while True:
        try:
            evaluate_market_symbols()
            monitor_positions()
            check_time_based_reports()
        except Exception as e:
            print(f"Background loop error: {e}")
        time.sleep(30)

if __name__ == '__main__':
    t = threading.Thread(target=background_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
