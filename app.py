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
HARD_STOP_PCT   = 0.12 / LEVERAGE   # ۱۲٪ ضرر با اهرم (هارد استاپ اضطراری)

# متغیرهای کنترل
last_traded_symbol = None
last_trade_direction = None
last_trade_time = 0
MIN_SECONDS_BETWEEN_TRADES = 45


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
    چک کردن پوزیشن‌های باز و بستن آن‌هایی که به ۱۲٪ ضرر رسیده‌اند
    """
    for p in positions:
        try:
            symbol = p.get('symbol')
            side = p.get('side', '').lower()          # 'long' یا 'short'
            contracts = float(p.get('contracts', 0) or 0)
            entry_price = float(p.get('entryPrice', 0) or 0)
            
            if contracts <= 0 or entry_price <= 0:
                continue

            # قیمت فعلی
            ticker = exchange.fetch_ticker(symbol)
            current_price = float(ticker['last'])

            # محاسبه درصد حرکت قیمت
            if side in ['long', 'buy']:
                price_change_pct = (current_price - entry_price) / entry_price
            else:  # short
                price_change_pct = (entry_price - current_price) / entry_price

            # اگر ضرر بیشتر از HARD_STOP_PCT شد → ببند
            if price_change_pct <= -HARD_STOP_PCT:
                print(f"🚨 Hard Stop triggered for {symbol} | side={side} | change={price_change_pct*100:.3f}%")
                
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
                    f"🛑 <b>هارد استاپ فعال شد</b>\n"
                    f"نماد: <b>{symbol}</b>\n"
                    f"جهت: {side}\n"
                    f"ورود: {entry_price:.6f}\n"
                    f"قیمت فعلی: {current_price:.6f}\n"
                    f"ضرر تقریبی: <b>{abs(price_change_pct)*100:.2f}%</b>\n"
                    f"پوزیشن به صورت اجباری بسته شد (حداکثر ۱۲٪ ضرر)."
                )
        except Exception as e:
            print(f"Error in hard stop check for position: {e}")


def run_bot_loop():
    global last_traded_symbol, last_trade_direction, last_trade_time
    time.sleep(10)
    send_telegram("🚀 <b>ربات اسکالپ (ایزوله + هارد استاپ ۱۲٪ + بدون بستن اضطراری) استارت شد!</b>")
    
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
                
                # --- هارد استاپ ۱۲٪ ---
                check_and_hard_stop(exchange, active_positions)
                
                # گزارش دوره‌ای
                if time.time() - last_status_report > 120:
                    msg = "📊 <b>وضعیت فعلی پوزیشن‌ها:</b>\n"
                    for p in active_positions:
                        symbol = p.get('symbol', 'N/A')
                        side = p.get('side', 'N/A')
                        entry = float(p.get('entryPrice', 0) or 0)
                        unrealized = float(p.get('unrealizedPnl', 0) or 0)
                        margin_mode = p.get('marginMode') or p.get('info', {}).get('marginMode', 'N/A')
                        msg += f"• {symbol} | {side} | ورود: {entry:.4f} | سود/ضرر: <b>{unrealized:.4f}</b> | مود: {margin_mode}\n"
                    send_telegram(msg)
                    last_status_report = time.time()

                if previous_active_count > 0 and current_active_count < previous_active_count:
                    send_telegram("🔔 <b>یک یا چند پوزیشن بسته شد.</b>\nدر حال اسکن مجدد کل بازار...")
                    time.sleep(20)

                previous_active_count = current_active_count
                time.sleep(8)   # چک سریع‌تر برای هارد استاپ
                continue

            # اگر پوزیشنی باز نیست
            if previous_active_count > 0 and current_active_count == 0:
                send_telegram("✅ همه پوزیشن‌ها بسته شدند. شروع اسکن کامل بازار...")
                previous_active_count = 0

            if time.time() - last_trade_time < MIN_SECONDS_BETWEEN_TRADES:
                time.sleep(10)
                continue

            # ========== اسکن بازار ==========
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
                
            traded_successfully = False
            
            for symbol, change, vol in candidates[:15]:
                try:
                    df_5m, df_10m = fetch_and_resample_candles(exchange, symbol)
                    if df_5m is None or len(df_5m) < 20 or df_10m is None or len(df_10m) < 10:
                        continue
                        
                    prev_high = df_5m['high'].iloc[-3]
                    prev_low = df_5m['low'].iloc[-3]
                    c_close_5m = df_5m['close'].iloc[-2]
                    
                    df_5m['ema5'] = df_5m['close'].ewm(span=5, adjust=False).mean()
                    df_5m['ema13'] = df_5m['close'].ewm(span=13, adjust=False).mean()
                    df_5m['rsi'] = calculate_rsi(df_5m['close'], period=14)
                    
                    c_rsi_5m = df_5m['rsi'].iloc[-2]
                    ema5_5m = df_5m['ema5'].iloc[-2]
                    ema13_5m = df_5m['ema13'].iloc[-2]
                    
                    long_fib_5m, short_fib_5m, _, _ = calculate_fibonacci_levels(df_5m)
                    
                    c_10m = df_10m.iloc[-2]
                    o_10m, h_10m, l_10m, cls_10m = c_10m['open'], c_10m['high'], c_10m['low'], c_10m['close']
                    range_10m = h_10m - l_10m
                    
                    upper_wick_10m = (h_10m - max(o_10m, cls_10m)) / range_10m if range_10m > 0 else 0
                    lower_wick_10m = (min(o_10m, cls_10m) - l_10m) / range_10m if range_10m > 0 else 0
                    
                    side = None
                    
                    if c_close_5m <= prev_low * 1.002:
                        side = 'buy'
                    elif c_close_5m >= prev_high * 0.998:
                        side = 'sell'
                    elif ema5_5m > ema13_5m and df_5m['ema5'].iloc[-3] <= df_5m['ema13'].iloc[-3]:
                        side = 'buy'
                    elif ema5_5m < ema13_5m and df_5m['ema5'].iloc[-3] >= df_5m['ema13'].iloc[-3]:
                        side = 'sell'
                    elif long_fib_5m and long_fib_5m['0.618'] <= c_close_5m <= long_fib_5m['0.382']:
                        side = 'buy'
                    elif short_fib_5m and short_fib_5m['0.382'] <= c_close_5m <= short_fib_5m['0.618']:
                        side = 'sell'
                    elif lower_wick_10m >= 0.35:
                        side = 'buy'
                    elif upper_wick_10m >= 0.35:
                        side = 'sell'
                    elif c_rsi_5m < 30:
                        side = 'buy'
                    elif c_rsi_5m > 70:
                        side = 'sell'

                    if not side:
                        continue
                        
                    if symbol == last_traded_symbol and side == last_trade_direction:
                        continue
                        
                    # تنظیم مود ایزوله + اهرم
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
                        
                    # ثبت سفارش بازار
                    if side == 'buy':
                        order = exchange.create_market_buy_order(symbol, amount)
                    else:
                        order = exchange.create_market_sell_order(symbol, amount)

                    entry_price = float(order.get('average') or order.get('price') or price)

                    # ثبت TP و SL (اگر نشد هم پوزیشن را نگه می‌داریم)
                    tp_sl_success, tp_price, sl_price = place_tp_sl_orders(
                        exchange, symbol, side, amount, entry_price
                    )

                    if tp_sl_success:
                        send_telegram(
                            f"🎯 <b>پوزیشن جدید باز شد (ایزوله + TP/SL تأییدشده)</b>\n"
                            f"نماد: <b>{symbol}</b>\n"
                            f"جهت: <b>{'خرید (Long)' if side == 'buy' else 'فروش (Short)'}</b>\n"
                            f"قیمت ورود: <b>{entry_price:.6f}</b>\n"
                            f"حد سود: <b>{tp_price:.6f}</b>\n"
                            f"حد ضرر: <b>{sl_price:.6f}</b>\n"
                            f"اهرم: {LEVERAGE}x | مود: Isolated"
                        )
                    else:
                        # فقط هشدار می‌دهیم، پوزیشن را نمی‌بندیم
                        send_telegram(
                            f"⚠️ <b>پوزیشن باز شد اما TP/SL کامل ثبت نشد</b>\n"
                            f"نماد: <b>{symbol}</b>\n"
                            f"جهت: <b>{'خرید (Long)' if side == 'buy' else 'فروش (Short)'}</b>\n"
                            f"قیمت ورود: <b>{entry_price:.6f}</b>\n"
                            f"⚠️ پوزیشن باز ماند. هارد استاپ ۱۲٪ فعال است و ربات خودش نظارت می‌کند."
                        )
                    
                    last_traded_symbol = symbol
                    last_trade_direction = side
                    last_trade_time = time.time()
                    traded_successfully = True
                    previous_active_count = 1
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
    return "Scalp Bot (Isolated + Hard Stop 12% + No Emergency Close) is running!"


if __name__ == '__main__':
    t = threading.Thread(target=run_bot_loop)
    t.daemon = True
    t.start()
    
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)
