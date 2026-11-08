import time
import datetime
import ccxt
import pandas as pd
import threading
import os
from flask import Flask

app = Flask(__name__)

# خواندن امن کلیدها از متغیرهای محیطی هاست ابری (Render)
API_KEY = os.getenv('TOOBIT_API_KEY')
SECRET_KEY = os.getenv('TOOBIT_SECRET_KEY')

exchange = ccxt.toobit({
    'apiKey': API_KEY,
    'secret': SECRET_KEY,
    'enableRateLimit': True,
    'options': {'defaultType': 'swap'}  # فیوچرز دائمی
})

TIMEFRAME = '1m'
LEVERAGE = 15
MAX_MARGIN = 0.5
FEE_RATE = 0.0006
MIN_NET_PROFIT = 0.01

positions = []  
current_symbol = None
blacklisted_symbols = [] 
last_trade = {'symbol': None, 'side': None}

@app.route('/')
def home():
    return "Toobit Trading Bot is running 24/7!", 200

def check_data_connection():
    try:
        exchange.check_required_credentials()
        markets = exchange.load_markets()
        if markets:
            print("✅ موفقیت: ارتباط با صرافی توبیت برقرار شد.")
            return True
        return False
    except Exception as e:
        print(f"❌ خطا در ارتباط با صرافی: {e}")
        return False

def analyze_market(symbol):
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, limit=30)
        df = pd.DataFrame(ohlcv, columns=['time', 'open', 'high', 'low', 'close', 'volume'])

        # محاسبه اندیکاتور EMA (میانگین متحرک نمایی)
        df['ema_fast'] = df['close'].ewm(span=5, adjust=False).mean()
        df['ema_slow'] = df['close'].ewm(span=13, adjust=False).mean()

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        gold_cross = (prev['ema_fast'] <= prev['ema_slow']) and (curr['ema_fast'] > curr['ema_slow'])
        death_cross = (prev['ema_fast'] >= prev['ema_slow']) and (curr['ema_fast'] < curr['ema_slow'])

        if gold_cross:
            return 'LONG'
        elif death_cross:
            return 'SHORT'
        elif curr['close'] > curr['ema_fast']:
            return 'LONG'
        else:
            return 'SHORT'
    except Exception as e:
        print(f"⚠️ خطا در تحلیل ارز {symbol}: {e}")
    return 'WAIT'

def find_most_volatile_symbol():
    try:
        markets = exchange.load_markets()
        tickers = exchange.fetch_tickers()
        valid_symbols = {}

        for symbol, ticker in tickers.items():
            if symbol in markets and symbol not in blacklisted_symbols:
                market_info = markets[symbol]
                is_swap = market_info.get('swap', False) or market_info.get('type') == 'swap'
                is_active = market_info.get('active', True)

                if is_swap and is_active:
                    change = ticker.get('percentage', 0)
                    quote_volume = ticker.get('quoteVolume', 0)
                    if change is not None and quote_volume > 50000:
                        valid_symbols[symbol] = abs(change)

        if not valid_symbols:
            return None

        sorted_symbols = sorted(valid_symbols, key=valid_symbols.get, reverse=True)

        for symbol in sorted_symbols:
            if symbol == last_trade['symbol']:
                temp_signal = analyze_market(symbol)
                if temp_signal == last_trade['side']:
                    continue
            return symbol

        return sorted_symbols[0] if sorted_symbols else None
    except Exception as e:
        print(f"⚠️ خطا در اسکن بازار: {e}")
        return None

def manage_open_positions(current_price):
    global positions, current_symbol
    if not positions:
        return

    for pos in positions[:]:
        side = pos['side']
        entry_price = pos['entry_price']
        open_time = pos['open_time']
        margin = pos['margin']
        elapsed_minutes = (time.time() - open_time) / 60

        if side == 'LONG':
            pnl_pct = ((current_price - entry_price) / entry_price) * 100 * LEVERAGE
            gross_pnl_usd = margin * (((current_price - entry_price) / entry_price) * LEVERAGE)
        else:
            pnl_pct = ((entry_price - current_price) / entry_price) * 100 * LEVERAGE
            gross_pnl_usd = margin * (((entry_price - current_price) / entry_price) * LEVERAGE)

        total_position_size_usd = margin * LEVERAGE
        total_fee = total_position_size_usd * FEE_RATE * 2
        net_est_profit = gross_pnl_usd - total_fee

        if net_est_profit < 0:
            pos['has_been_in_loss'] = True

        if pnl_pct >= 10.0 or pnl_pct <= -5.0:
            close_specific_position(pos, current_price)
            continue

        if pos.get('has_been_in_loss', False) and net_est_profit >= MIN_NET_PROFIT:
            close_specific_position(pos, current_price)
            continue

        if 7 <= elapsed_minutes <= 9 and net_est_profit < 0:
            close_specific_position(pos, current_price)
            continue

def open_position(side, current_price, symbol):
    global positions
    try:
        try:
            exchange.set_leverage(LEVERAGE, symbol)
        except Exception:
            pass
        
        markets = exchange.load_markets()
        market = markets.get(symbol, {})
        limits = market.get('limits', {})
        min_amount = limits.get('amount', {}).get('min', None)

        notional_value_usd = MAX_MARGIN * LEVERAGE  
        amount = notional_value_usd / current_price

        if min_amount is not None and amount < min_amount:
            return False

        amount = float(exchange.amount_to_precision(symbol, amount))
        order_side = 'buy' if side == 'LONG' else 'sell'
        
        print(f"🚀 [ثبت سفارش] ارز: {symbol} | جهت: {side} | حجم: {amount}")
        order = exchange.create_market_order(symbol, order_side, amount)
        actual_entry_price = order.get('average', current_price) or current_price

        new_pos = {
            'side': side,
            'entry_price': actual_entry_price,
            'open_time': time.time(),
            'type': 'MAIN',
            'has_been_in_loss': False,
            'margin': MAX_MARGIN,
            'amount': amount
        }
        positions.append(new_pos)
        return True
    except Exception as e:
        print(f"❌ خطا در ثبت سفارش روی {symbol}: {e}")
        return False

def close_specific_position(pos, exit_price):
    global positions, last_trade, current_symbol
    entry_price = pos['entry_price']
    side = pos['side']
    margin = pos['margin']
    amount = pos['amount']
    symbol = current_symbol

    try:
        close_side = 'sell' if side == 'LONG' else 'buy'
        exchange.create_market_order(symbol, close_side, amount, params={'reduceOnly': True})
        print(f"🔒 پوزیشن بسته شد.")
    except Exception as e:
        print(f"⚠️ خطا در بستن پوزیشن: {e}")

    last_trade['symbol'] = symbol
    last_trade['side'] = side
    positions.remove(pos)

def bot_loop():
    global current_symbol
    if not check_data_connection():
        print("⛔ خطا در اتصال صرافی.")
        return

    print("⚡ ربات خودکار تحلیل و معامله گر فعال شد...")
    while True:
        try:
            if len(positions) == 0:
                current_symbol = find_most_volatile_symbol()
                if not current_symbol:
                    time.sleep(10)
                    continue

                ticker = exchange.fetch_ticker(current_symbol)
                current_price = ticker['last']

                signal = analyze_market(current_symbol)
                if signal in ['LONG', 'SHORT']:
                    open_position(signal, current_price, current_symbol)
                
                time.sleep(2)
            else:
                ticker = exchange.fetch_ticker(current_symbol)
                current_price = ticker['last']
                manage_open_positions(current_price)

            time.sleep(3)
        except Exception as e:
            print(f"⚠️ خطا در حلقه ربات: {e}")
            time.sleep(5)

if __name__ == '__main__':
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=5000)
