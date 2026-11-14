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
hourly_stats = {
    'total_trades': 0,
    'wins': 0,
    'losses': 0,
    'net_pnl_usd': 0.0
}
daily_stats = {
    'total_trades': 0,
    'wins': 0,
    'losses': 0,
    'net_pnl_usd': 0.0
}
last_hourly_report_hour = datetime.datetime.now().hour
last_daily_report_day = datetime.datetime.now().day

def send_telegram(message):
    """ارسال پیام به تلگرام"""
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
    """دریافت کندل‌های قیمتی از بایننس (بدون نیاز به کلید صرافی)"""
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
    low_close = (df['low'] - df['close'].shift()).abs()
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    df['atr'] = tr.rolling(window=14).mean()
    
    return df

@app.route('/')
def home():
    return "Cyclo Trading Bot with Advanced Telegram Reporting is Running! 🚀"

def evaluate_market_symbols():
    """اسکن بازار بر اساس شرایط استراتژی"""
    global active_positions
    if len(active_positions) > 0:
        return

    symbols = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'ADAUSDT', 'XRPUSDT', 'DOGEUSDT']
    
    for symbol in symbols:
        df = get_ohlcv(symbol, limit=40)
        if df is None or len(df) < 25:
            continue
            
        df = calculate_indicators(df)
        curr = df.iloc[-1]
        prev = df.iloc[-2]
        
        crossover = (prev['ema_fast'] <= prev['ema_slow']) and (curr['ema_fast'] > curr['ema_slow'])
        is_volume_high = curr['volume'] > curr['vol_ma']
        rsi_valid = curr['rsi'] > 45
        
        if crossover and is_volume_high and rsi_valid:
            entry_price = curr['close']
            stop_loss_level = entry_price - (curr['atr'] * 2.0)
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
        f"🚀 <b>پوزیشن خرید جدید باز شد</b>\n"
        f"پلتفرم: <b>شبیه‌ساز (Paper Trading)</b>\n"
        f"نماد: <b>{symbol}</b> | اهرم: <b>{LEVERAGE}x</b>\n"
        f"قیمت ورود: <b>{entry_price:.4f}</b>\n"
        f"حد ضرر ATR: {stop_loss_level:.4f}"
    )

def monitor_positions():
    """مدیریت پوزیشن‌های باز و بررسی سود، زیان و شرایط خروج"""
    global active_positions, hourly_stats, daily_stats
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
        
        pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
        
        if pnl_pct > pos['max_pnl_seen']:
            pos['max_pnl_seen'] = pnl_pct
        if pnl_pct < 0:
            pos['has_been_in_loss'] = True

        crossunder = (prev['ema_fast'] >= prev['ema_slow']) and (curr['ema_fast'] < curr['ema_slow'])
        hit_atr_sl = current_price < stop_loss_level
        rsi_exit = rsi_value < 40

        if crossunder or hit_atr_sl or rsi_exit:
            reason = "برخورد به حد ضرر ATR 🛑" if hit_atr_sl else ("تقاطع نزولی EMA 📉" if crossunder else "افت RSI زیر ۴۰ 🔻")
            close_position(pos, current_price, reason)
            continue

        if pnl_pct >= TAKE_PROFIT_PCT:
            close_position(pos, current_price, "هدف سود ۱۰ درصدی محقق شد 🎯")
            continue
            
        if pnl_pct <= -MAX_LOSS_PCT:
            close_position(pos, current_price, f"حد ضرر نهایی ({MAX_LOSS_PCT}%) 🛑")
            continue

        if elapsed_minutes >= 5 and pnl_pct >= 0.01 and pos['max_pnl_seen'] < 2.0:
            close_position(pos, current_price, "بازار راکد - ذخیره سود جزئی 🟢")
            continue

        if elapsed_minutes >= MAX_HOLD_MINUTES:
            close_position(pos, current_price, "پایان زمان مجاز ۲۰ دقیقه‌ای ⏰")
            continue

def close_position(pos, exit_price, reason):
    global active_positions, hourly_stats, daily_stats
    if pos in active_positions:
        active_positions.remove(pos)
        
    entry_price = pos['entry_price']
    duration = round((time.time() - pos['open_time']) / 60, 1)
    pnl_pct = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE
    
    # محاسبه سود یا زیان دلاری بر اساس مارجین و اهرم
    pnl_usd = pos['margin'] * (pnl_pct / 100)

    # آپدیت آمار ساعتی و روزانه
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
        f"{emoji} <b>گزارش اتمام معامله</b>\n"
        f"نماد: <b>{pos['symbol']}</b>\n"
        f"دلیل خروج: <b>{reason}</b>\n"
        f"قیمت ورود: {entry_price:.4f} ➔ قیمت خروج: {exit_price:.4f}\n"
        f"مدت معامله: {duration} دقیقه\n"
        f"سود/زیان درصدی (اهرم {LEVERAGE}x): <b>{pnl_pct:+.2f}%</b>\n"
        f"مبلغ سود/زیان دلاری: <b>${pnl_usd:+.2f}</b>"
    )

def check_time_based_reports():
    """بررسی و ارسال گزارش‌های سر ساعت و گزارش شبانه"""
    global hourly_stats, daily_stats, last_hourly_report_hour, last_hourly_report_day
    
    now = datetime.datetime.now()
    current_hour = now.hour
    current_day = now.day

    # ارسال گزارش سر ساعت
    if current_hour != last_hourly_report_hour:
        last_hourly_report_hour = current_hour
        msg = (
            f"📊 <b>گزارش عملکرد ساعتی (ساعت {current_hour}:00)</b>\n"
            f"• تعداد معاملات این ساعت: <b>{hourly_stats['total_trades']}</b>\n"
            f"• معاملات موفق: {hourly_stats['wins']} | ناموفق: {hourly_stats['losses']}\n"
            f"• مجموع سود/زیان خالص این ساعت: <b>${hourly_stats['net_pnl_usd']:+.2f}</b>"
        )
        send_telegram(msg)
        # ریست کردن آمار ساعتی
        hourly_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}

    # ارسال گزارش آخر شب (ساعت ۲۳:۵۹ یا تغییر روز)
    if current_day != last_hourly_report_day and current_hour == 0:
        last_hourly_report_day = current_day
        msg = (
            f"🌙 <b>گزارش پایانی و جمع‌بندی آخر شب</b>\n"
            f"📅 تاریخ: {(now - datetime.timedelta(days=1)).strftime('%Y-%m-%d')}\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"• کل معاملات امروز: <b>{daily_stats['total_trades']}</b>\n"
            f"• مجموع برنده: {daily_stats['wins']} | مجموع بازنده: {daily_stats['losses']}\n"
            f"• 💰 <b>سود/زیان کل امروز: ${daily_stats['net_pnl_usd']:+.2f}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━\n"
            f"<i>ربات برای روز جدید آماده است. 🚀</i>"
        )
        send_telegram(msg)
        # ریست کردن آمار روزانه
        daily_stats = {'total_trades': 0, 'wins': 0, 'losses': 0, 'net_pnl_usd': 0.0}

def background_loop():
    # پیام اولیه که اعلام می‌کند ربات استارت شده است
    send_telegram(
        "🟢 <b>ربات معاملاتی و مانیتورینگ استارت شد!</b>\n"
        "• وضعیت اتصال: <b>برقرار و فعال</b>\n"
        "• حالت: <b>شبیه‌ساز هوشمند (Paper Trading)</b>\n"
        "• استراتژی: <b>سیکلو (EMA + RSI + ATR + حجم)</b>\n"
        "<i>آماده رصد بازار و ارسال گزارش معاملات...</i>"
    )

    while True:
        try:
            evaluate_market_symbols()
            monitor_positions()
            check_time_based_reports()
        except Exception as e:
            print(f"Background loop error: {e}")
        time.sleep(15)

if __name__ == '__main__':
    t = threading.Thread(target=background_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
