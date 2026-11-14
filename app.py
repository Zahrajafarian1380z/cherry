import os
import time
import random
import threading
import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

# --- تنظیمات تلگرام ---
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_CHAT_ID")

# --- تنظیمات مدیریت ریسک و اهرم ---
LEVERAGE = 15
INITIAL_MARGIN = 0.2         # مارجین فرضی هر پوزیشن (دلار)
DEFAULT_TP_PCT = 12.0        # تارگت سود ایده‌آل (۱۲٪ با اهرم)
MAX_LOSS_PCT = 22.0          # حداکثر ضرر مجاز (فورس استاپ ۲۲٪)
MAX_HOLD_MINUTES = 20        # حداکثر زمان باز ماندن پوزیشن (۲۰ دقیقه)

# صف انتظار برای شکار قیمت بهتر و لیست پوزیشن‌های فعال
pending_signals = []
active_positions = []

def send_telegram(message):
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

def get_live_price(symbol):
    """دریافت قیمت لحظه‌ای ارز از API عمومی (بدون نیاز به حساب صرافی)"""
    clean_symbol = symbol.replace('/', '').replace(':USDT', 'USDT').replace('.P', '')
    if not clean_symbol.endswith('USDT'):
        clean_symbol += 'USDT'
    try:
        url = f"https://api.binance.com/api/v3/ticker/price?symbol={clean_symbol}"
        resp = requests.get(url, timeout=4).json()
        return float(resp['price'])
    except:
        return None

@app.route('/')
def home():
    return "Smart Paper-Trading Bot (Delayed Entry & Dynamic Exit) is Running! 🚀"

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.json
        if not data:
            return jsonify({"status": "error", "message": "No data"}), 400

        symbol = data.get('ticker', 'UNKNOWN')
        action = data.get('action', 'buy').lower()
        signal_price = float(data.get('price', 0))

        if signal_price <= 0:
            return jsonify({"status": "error", "message": "Invalid price"}), 400

        side = 'LONG' if action in ['buy', 'long'] else 'SHORT'
        
        # تعیین زمان انتظار بین ۳ تا ۷ دقیقه (۱۸۰ تا ۴۲۰ ثانیه)
        wait_seconds = random.randint(180, 420)
        wait_minutes = round(wait_seconds / 60, 1)

        pending_item = {
            'symbol': symbol,
            'side': side,
            'signal_price': signal_price,
            'best_price_seen': signal_price,
            'received_time': time.time(),
            'target_entry_time': time.time() + wait_seconds,
            'wait_minutes': wait_minutes
        }
        pending_signals.append(pending_item)

        send_telegram(
            f"⏳ <b>سیگنال جدید در صف انتظار قرار گرفت</b>\n"
            f"نماد: <b>{symbol}</b> | جهت: <b>{side}</b>\n"
            f"قیمت سیگنال: {signal_price:.6f}\n"
            f"مدت بررسی برای شکار قیمت بهتر: <b>{wait_minutes} دقیقه</b>"
        )

        return jsonify({"status": "queued", "wait_minutes": wait_minutes}), 200
    except Exception as e:
        print(f"Webhook Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500

def process_pending_signals():
    """بررسی سیگنال‌های در حال انتظار برای ورود در قیمت بهتر"""
    global pending_signals, active_positions

    for sig in pending_signals[:]:
        symbol = sig['symbol']
        side = sig['side']
        sig_price = sig['signal_price']
        now = time.time()

        current_price = get_live_price(symbol) or sig['best_price_seen']

        # ثبت بهترین قیمت مشاهده‌شده در طول بازه انتظار
        if side == 'LONG' and current_price < sig['best_price_seen']:
            sig['best_price_seen'] = current_price
        elif side == 'SHORT' and current_price > sig['best_price_seen']:
            sig['best_price_seen'] = current_price

        # محاسبه میزان بهتر شدن قیمت (بر حسب درصد)
        if side == 'LONG':
            improvement_pct = ((sig_price - current_price) / sig_price) * 100
        else:
            improvement_pct = ((current_price - sig_price) / sig_price) * 100

        # شرط ورود: یا حداقل ۰.۲۵٪ قیمت بهتر شده باشد (بعد از گذشت ۳ دقیقه) یا زمان انتظار تمام شده باشد
        min_time_passed = (now - sig['received_time']) >= 180
        time_expired = now >= sig['target_entry_time']

        if (min_time_passed and improvement_pct >= 0.25) or time_expired:
            pending_signals.remove(sig)
            open_paper_position(symbol, side, current_price, sig_price)

def open_paper_position(symbol, side, entry_price, signal_price):
    global active_positions

    new_pos = {
        'symbol': symbol,
        'side': side,
        'entry_price': entry_price,
        'signal_price': signal_price,
        'open_time': time.time(),
        'margin': INITIAL_MARGIN,
        'max_pnl_seen': 0.0,
        'has_been_in_loss': False
    }
    active_positions.append(new_pos)

    diff_pct = abs((entry_price - signal_price) / signal_price) * 100
    better_note = "قیمت بهتر شکار شد ✅" if (
        (side == 'LONG' and entry_price < signal_price) or 
        (side == 'SHORT' and entry_price > signal_price)
    ) else "ورود پس از پایان زمان انتظار ⏱"

    send_telegram(
        f"🎯 <b>پوزیشن کاغذی باز شد ({better_note})</b>\n"
        f"نماد: <b>{symbol}</b> | جهت: <b>{side}</b> (اهرم {LEVERAGE}x)\n"
        f"قیمت سیگنال: {signal_price:.6f} ➔ قیمت ورود: <b>{entry_price:.6f}</b>\n"
        f"اختلاف قیمت ورود: {diff_pct:.2f}%"
    )

def monitor_active_positions():
    """مدیریت هوشمند پوزیشن‌های باز بر اساس شرایط بازار، رکود و محدودیت ۲۰ دقیقه‌ای"""
    global active_positions

    for pos in active_positions[:]:
        symbol = pos['symbol']
        side = pos['side']
        entry_price = pos['entry_price']
        elapsed_minutes = (time.time() - pos['open_time']) / 60

        current_price = get_live_price(symbol)
        if not current_price:
            continue

        # محاسبه درصد سود/زیان با اهرم ۱۵
        if side == 'LONG':
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE

        if pnl_pct > pos['max_pnl_seen']:
            pos['max_pnl_seen'] = pnl_pct
        if pnl_pct < 0:
            pos['has_been_in_loss'] = True

        # ۱. قانون سقف زمانی ۲۰ دقیقه (مهم‌ترین محدودیت زمانی)
        if elapsed_minutes >= MAX_HOLD_MINUTES:
            close_paper_position(pos, current_price, "پایان زمان مجاز ۲۰ دقیقه‌ای ⏰")
            continue

        # ۲. حد ضرر نهایی ۲۲٪ (فورس استاپ)
        if pnl_pct <= -MAX_LOSS_PCT:
            close_paper_position(pos, current_price, f"توقف ضرر حداکثری ({MAX_LOSS_PCT}%) 🛑")
            continue

        # ۳. تارگت سود ایده‌آل (۱۲٪ با اهرم)
        if pnl_pct >= DEFAULT_TP_PCT:
            close_paper_position(pos, current_price, "رسیدن به هدف سود کامل 🎯")
            continue

        # ۴. تشخیص بازار راکد و قانع بودن به سود ۰.۰۱٪
        # اگر بیش از ۵ دقیقه گذشته، نوسان شدیدی نداشته و سود حداقل 0.01% شده است:
        if elapsed_minutes >= 5 and pnl_pct >= 0.01 and pos['max_pnl_seen'] < 2.5:
            close_paper_position(pos, current_price, "بازار راکد - ذخیره سود جزئی (۰.۰۱٪+) 🟢")
            continue

        # اگر پوزیشن قبلاً در ضرر بوده و الان سر به سر یا با سود ۰.۰۱٪ برگشته است:
        if pos['has_been_in_loss'] and elapsed_minutes >= 4 and pnl_pct >= 0.01:
            close_paper_position(pos, current_price, "بازگشت از ضرر به سود جزئی (۰.۰۱٪+) 🔄")
            continue

        # ۵. تشخیص شرایط بد بازار و خروج با ضرر کم قبل از فاجعه
        # اگر بعد از ۸ دقیقه معامله همچنان در ضرر است و ضرر بین ۳٪ تا ۶٪ است (بازار خلاف جهت ما قفل کرده):
        if elapsed_minutes >= 8 and -6.0 <= pnl_pct <= -2.5:
            close_paper_position(pos, current_price, "شرایط نامساعد بازار - خروج پیشگیرانه با ضرر کم 🛡️")
            continue

def close_paper_position(pos, exit_price, reason):
    global active_positions
    if pos in active_positions:
        active_positions.remove(pos)

    side = pos['side']
    entry_price = pos['entry_price']
    duration = round((time.time() - pos['open_time']) / 60, 1)

    if side == 'LONG':
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100 * LEVERAGE
    else:
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100 * LEVERAGE

    emoji = "✅" if pnl_pct >= 0 else "🔻"
    send_telegram(
        f"{emoji} <b>پوزیشن بسته شد</b>\n"
        f"دلیل: <b>{reason}</b>\n"
        f"نماد: <b>{pos['symbol']}</b> | جهت: {side}\n"
        f"ورود: {entry_price:.6f} ➔ خروج: {exit_price:.6f}\n"
        f"مدت معامله: {duration} دقیقه\n"
        f"نتیجه نهایی (اهرم {LEVERAGE}x): <b>{pnl_pct:+.2f}%</b>"
    )

def main_loop():
    while True:
        try:
            process_pending_signals()
            monitor_active_positions()
        except Exception as e:
            print(f"Loop error: {e}")
        time.sleep(4)

if __name__ == '__main__':
    t = threading.Thread(target=main_loop)
    t.daemon = True
    t.start()

    port = int(os.environ.get("PORT", 10000))
    app.run(host='0.0.0.0', port=port)
