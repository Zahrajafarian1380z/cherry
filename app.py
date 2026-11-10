import time

# تنظیمات مطابق کد اصلی
LEVERAGE = 15
MAX_MARGIN = 0.3          # حداقل حجم ۰.۳ دلار
TAKE_PROFIT_PCT = 0.10 / LEVERAGE   # ۱۰٪ سود با اهرم
STOP_LOSS_PCT   = 0.05 / LEVERAGE   # ۵٪ ضرر با اهرم

def place_tp_sl_orders(exchange, symbol, side, amount, entry_price):
    """
    ثبت مطمئن سفارش‌های حد سود و حد ضرر
    دقیقاً مطابق قوانین کد اصلی:
    - TP = 10% سود با اهرم
    - SL = 5% ضرر با اهرم
    """
    if side == 'buy' or side == 'LONG':
        tp_price = entry_price * (1 + TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 - STOP_LOSS_PCT)
        close_side = 'sell'
    else:
        tp_price = entry_price * (1 - TAKE_PROFIT_PCT)
        sl_price = entry_price * (1 + STOP_LOSS_PCT)
        close_side = 'buy'

    # رند کردن قیمت‌ها و حجم بر اساس precision بازار
    try:
        market = exchange.market(symbol)
        tp_price = float(exchange.price_to_precision(symbol, tp_price))
        sl_price = float(exchange.price_to_precision(symbol, sl_price))
        amount = float(exchange.amount_to_precision(symbol, amount))
    except:
        pass

    tp_ok = False
    sl_ok = False
    last_error = ""

    # روش‌های مختلف ثبت سفارش (به ترتیب اولویت)
    methods = [
        # روش ۱: take_profit_market + stop_market
        {
            'tp': {'type': 'take_profit_market', 'params': {'stopPrice': tp_price, 'reduceOnly': True}},
            'sl': {'type': 'stop_market', 'params': {'stopPrice': sl_price, 'reduceOnly': True}}
        },
        # روش ۲: stop با triggerPrice
        {
            'tp': {'type': 'limit', 'params': {'reduceOnly': True}},
            'sl': {'type': 'stop', 'params': {'stopPrice': sl_price, 'reduceOnly': True}}
        },
        # روش ۳: سفارش‌های ساده limit با reduceOnly
        {
            'tp': {'type': 'limit', 'params': {'reduceOnly': True}},
            'sl': {'type': 'limit', 'params': {'reduceOnly': True}}
        }
    ]

    for method in methods:
        try:
            # ثبت Take Profit
            if not tp_ok:
                if method['tp']['type'] == 'limit':
                    exchange.create_order(symbol, 'limit', close_side, amount, tp_price, method['tp']['params'])
                else:
                    exchange.create_order(symbol, method['tp']['type'], close_side, amount, None, method['tp']['params'])
                tp_ok = True

            # ثبت Stop Loss
            if not sl_ok:
                if method['sl']['type'] == 'limit':
                    exchange.create_order(symbol, 'limit', close_side, amount, sl_price, method['sl']['params'])
                else:
                    exchange.create_order(symbol, method['sl']['type'], close_side, amount, None, method['sl']['params'])
                sl_ok = True

            if tp_ok and sl_ok:
                break

        except Exception as e:
            last_error = str(e)
            print(f"TP/SL method failed: {e}")
            time.sleep(1)

    # چک نهایی: آیا واقعاً سفارش‌های باز داریم؟
    try:
        open_orders = exchange.fetch_open_orders(symbol)
        has_reduce_only = any(
            o.get('reduceOnly') or o.get('info', {}).get('reduceOnly') 
            for o in open_orders
        )
        if not has_reduce_only:
            tp_ok = False
            sl_ok = False
    except:
        pass

    if tp_ok and sl_ok:
        print(f"✅ TP/SL ثبت شد | TP: {tp_price} | SL: {sl_price}")
        return True, tp_price, sl_price
    else:
        print(f"❌ Failed to place TP/SL. Last error: {last_error}")
        return False, tp_price, sl_price
