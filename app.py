import os
import math
import json
import datetime
import time
import threading
from flask import Flask, request, jsonify, render_template
import ccxt
import gspread

app = Flask(__name__)

WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')
GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]

try:
    EXCHANGE_CONFIG = json.loads(os.environ.get('EXCHANGE_CONFIG', '{}'))
except:
    EXCHANGE_CONFIG = {}

GOOGLE_CLIENT = None
CURRENT_EXCHANGE_NAME = None
EXCHANGE_INSTANCE = None

BOT_SETTINGS = {"e2_pct": 100.0, "f2_type": "MARKET", "j2_slip": 0.0, "active_symbol": ""}
BOT_STATE = {}
CACHE = {"wallet": {"USDT": 0.0}}
STATE_LOCK = threading.Lock()
LOG_QUEUE = []
THREADS = {"logger": None, "sync": None}

def load_exchange(config_name):
    global EXCHANGE_INSTANCE, CURRENT_EXCHANGE_NAME
    if config_name not in EXCHANGE_CONFIG: return False
    conf = EXCHANGE_CONFIG[config_name]
    try:
        ex_class = getattr(ccxt, conf.get('exchange_id', 'binance'))
        params = {
            'apiKey': conf.get('apiKey'),
            'secret': conf.get('secret'),
            'options': conf.get('options', {'defaultType': 'spot'})
        }
        if conf.get('sandbox', False):
            params['sandbox'] = True
            if conf['exchange_id'] == 'binance':
                params['urls'] = {'api': {'public': 'https://testnet.binance.vision/api', 'private': 'https://testnet.binance.vision/api'}}
        
        new_ex = ex_class(params)
        new_ex.load_markets()
        EXCHANGE_INSTANCE = new_ex
        CURRENT_EXCHANGE_NAME = config_name
        return True
    except Exception as e:
        print(f"Exchange Load Error: {e}")
        return False

if EXCHANGE_CONFIG:
    load_exchange(list(EXCHANGE_CONFIG.keys())[0])

def safe_float(value, default=0.0):
    try:
        if isinstance(value, str):
            clean = value.replace('$', '').replace(',', '').replace(' ', '').strip()
            if clean == "": return default
            return float(clean)
        return float(value)
    except: return default

def get_cached_balance(asset):
    with STATE_LOCK:
        return CACHE['wallet'].get(asset, 0.0)

def get_sheet():
    global GOOGLE_CLIENT
    try:
        if GOOGLE_CLIENT is None:
            creds = json.loads(GOOGLE_JSON)
            GOOGLE_CLIENT = gspread.service_account_from_dict(creds, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")
    except:
        creds = json.loads(GOOGLE_JSON)
        GOOGLE_CLIENT = gspread.service_account_from_dict(creds, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")

def normalize_symbol(user_input):
    if not EXCHANGE_INSTANCE: return user_input
    if user_input in EXCHANGE_INSTANCE.markets: return user_input
    for symbol, market in EXCHANGE_INSTANCE.markets.items():
        if market['id'] == user_input: return symbol
    clean = user_input.replace("/", "").upper()
    for symbol, market in EXCHANGE_INSTANCE.markets.items():
        if market['id'] == clean or symbol.replace("/", "") == clean: return symbol
    return user_input

def get_state(symbol):
    if symbol not in BOT_STATE:
        BOT_STATE[symbol] = {"status": "EMPTY", "pending_limit": False}
    return BOT_STATE[symbol]

def logger_worker_func():
    while True:
        if len(LOG_QUEUE) > 0:
            task_type, data = LOG_QUEUE[0]
            try:
                sheet = get_sheet()
                if task_type == 'LOG':
                    col_a = sheet.col_values(1)
                    nr = max(len(col_a) + 1, 6)
                    sheet.update(f'A{nr}:K{nr}', [data])
                LOG_QUEUE.pop(0)
            except: time.sleep(5)
        time.sleep(1)

def background_sync_func():
    time.sleep(2)
    tick = 0
    local_ex = None
    last_ex_name = ""
    
    while True:
        try:
            if CURRENT_EXCHANGE_NAME != last_ex_name and CURRENT_EXCHANGE_NAME:
                conf = EXCHANGE_CONFIG[CURRENT_EXCHANGE_NAME]
                ex_c = getattr(ccxt, conf.get('exchange_id', 'binance'))
                p = {'apiKey': conf.get('apiKey'), 'secret': conf.get('secret'), 'options': conf.get('options', {'defaultType': 'spot'})}
                if conf.get('sandbox', False):
                    p['sandbox'] = True
                    if conf['exchange_id'] == 'binance':
                         p['urls'] = {'api': {'public': 'https://testnet.binance.vision/api', 'private': 'https://testnet.binance.vision/api'}}
                local_ex = ex_c(p)
                last_ex_name = CURRENT_EXCHANGE_NAME

            sheet = None
            if (tick % 3 == 0) or (tick % 6 == 0):
                try: sheet = get_sheet()
                except: pass

            if tick % 3 == 0 and sheet:
                try:
                    data = sheet.batch_get(['E2', 'G2', 'K2', 'H1'])
                    val_e2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 100)
                    val_f2 = str(data[1][0][0]).upper() if (len(data)>1 and data[1]) else "MARKET"
                    val_j2 = safe_float(str(data[2][0][0]).replace("%", "") if (len(data)>2 and data[2]) else 0)
                    val_h1 = str(data[3][0][0]).strip().upper() if (len(data)>3 and data[3]) else ""
                    with STATE_LOCK:
                        BOT_SETTINGS.update({'e2_pct': val_e2, 'f2_type': val_f2, 'j2_slip': val_j2, 'active_symbol': val_h1})
                except: pass

            if tick % 2 == 0 and local_ex:
                try:
                    bal = local_ex.fetch_balance()
                    with STATE_LOCK:
                        CACHE['wallet'] = {}
                        for a, amt in bal['free'].items():
                            if amt > 0: CACHE['wallet'][a] = float(amt)
                        for a, amt in bal['total'].items():
                            if a == 'USDT': continue
                            sym = f"{a}/USDT" 
                            if amt > 0:
                                if sym not in BOT_STATE: BOT_STATE[sym] = {}
                                BOT_STATE[sym]['status'] = 'HOLDING'
                                BOT_STATE[sym]['pending_limit'] = (bal['used'].get(a, 0) > 0)
                            else:
                                if sym in BOT_STATE and BOT_STATE[sym]['status'] == 'HOLDING':
                                     BOT_STATE[sym]['status'] = 'EMPTY'
                                     BOT_STATE[sym]['pending_limit'] = False
                except: pass

            if tick % 6 == 0 and sheet:
                try:
                    usdt = CACHE['wallet'].get('USDT', 0)
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sheet.update('A2:B2', [[ts, usdt]])
                    ms = BOT_SETTINGS.get('active_symbol', '').replace("USDT","").replace("/","").strip()
                    if ms: sheet.update('I2', [[CACHE['wallet'].get(ms, 0.0)]])
                except: pass
        except: time.sleep(10)
        tick += 1
        time.sleep(3)

def ensure_threads_running():
    if THREADS["logger"] is None or not THREADS["logger"].is_alive():
        THREADS["logger"] = threading.Thread(target=logger_worker_func, daemon=True)
        THREADS["logger"].start()
    if THREADS["sync"] is None or not THREADS["sync"].is_alive():
        THREADS["sync"] = threading.Thread(target=background_sync_func, daemon=True)
        THREADS["sync"].start()

@app.route('/')
def home():
    ensure_threads_running()
    return f"Bot Active. Exchange: {CURRENT_EXCHANGE_NAME}", 200

@app.route('/special')
def special_ui():
    return render_template('panic.html')

@app.route('/special/verify', methods=['POST'])
def special_verify():
    if request.json.get('passphrase') == WEBHOOK_PASSPHRASE:
        return jsonify({"status": "ok", "target": BOT_SETTINGS.get('active_symbol', 'UNKNOWN')})
    return jsonify({"status": "error"}), 403

@app.route('/special/execute', methods=['POST'])
def special_execute():
    if request.json.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Auth Failed"}), 403
    raw_sym = request.json.get('symbol', BOT_SETTINGS.get('active_symbol', ''))
    if not raw_sym or raw_sym == "FORCE_SHEET": raw_sym = BOT_SETTINGS.get('active_symbol', '')
    
    symbol = normalize_symbol(raw_sym)
    base = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', '')
    log = []
    
    try:
        try:
            EXCHANGE_INSTANCE.cancel_all_orders(symbol)
            log.append(f"Orders cancelled: {symbol}")
        except: log.append("Cancel skipped.")
        
        time.sleep(1.0)
        bal = EXCHANGE_INSTANCE.fetch_balance()
        qty = bal['free'].get(base, 0.0)
        log.append(f"Free: {qty} {base}")
        
        resp = "Nothing to sell"
        if qty > 0:
            res = EXCHANGE_INSTANCE.create_order(symbol, 'market', 'sell', qty)
            log.append(f"SOLD {qty}")
            resp = str(res.get('id', 'Filled'))
            with STATE_LOCK:
                BOT_STATE[symbol]['status'] = 'EMPTY'
                BOT_STATE[symbol]['pending_limit'] = False
                CACHE['wallet'][base] = 0.0
        return jsonify({"status": "Done", "log": log, "order": resp})
    except Exception as e:
        return jsonify({"status": "Error", "log": [str(e)]})

@app.route('/webhook', methods=['POST'])
def webhook():
    ensure_threads_running()
    data = request.get_json(force=True)
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    if not EXCHANGE_INSTANCE: return jsonify({"error": "No Exchange"}), 500

    symbol = normalize_symbol(data['symbol'].upper())
    side = data['side'].lower()
    base = symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT','')
    price = data.get('price', 'Market')
    otype = data.get('type', 'MARKET').lower()
    reason = data.get('reason', '')
    is_cli = "CLI" in reason

    with STATE_LOCK:
        # Settings Override
        if not is_cli:
            e2 = BOT_SETTINGS['e2_pct']
            slip = BOT_SETTINGS['j2_slip']
            if otype == 'market' and 'LIMIT' in BOT_SETTINGS['f2_type']:
                if price != 'Market' and safe_float(price) > 0: otype = 'limit'
        else:
            e2, slip = 100.0, 0.0
        
        # State Check
        st = get_state(symbol)
        if not is_cli and side == 'buy' and st['status'] == 'HOLDING':
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            LOG_QUEUE.append(('LOG', [ts, symbol, side, "0%", price, "", 0, 0, "Skipped", "Already Holding", CACHE['wallet'].get('USDT', 0)]))
            return jsonify({"status": "skipped", "msg": "Already Holding"})
        
        if is_cli or st['pending_limit']:
            try: EXCHANGE_INSTANCE.cancel_all_orders(symbol)
            except: pass
            time.sleep(0.5)

    try:
        # Auto-Refresh Balance for CLI to ensure accuracy
        if is_cli:
            try:
                fresh = EXCHANGE_INSTANCE.fetch_balance()
                with STATE_LOCK:
                    CACHE['wallet']['USDT'] = fresh['free'].get('USDT', 0.0)
                    CACHE['wallet'][base] = fresh['free'].get(base, 0.0)
            except: pass

        resp = {}
        log_retry = ""

        # ==========================
        #       BUY LOGIC
        # ==========================
        if side == 'buy':
            wallet_usdt = get_cached_balance("USDT")
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2)))
            
            # Safety buffer: 99.8% to allow for fees/rounding
            amt_usdt = wallet_usdt * (0.998 if req_pct >= 99.0 else req_pct / 100.0)
            if amt_usdt < 5: raise Exception(f"Insufficient USDT: {amt_usdt:.2f}")

            # Define execution function to allow retry
            def execute_buy(usdt_amount):
                if otype == 'limit':
                    # 1. Calculate Limit Price
                    cur_p = safe_float(price)
                    if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                    
                    lim_p = float(data.get('limit_price', cur_p))
                    if lim_p == 0: lim_p = cur_p
                    lim_p *= (1 + (slip / 100.0))
                    
                    # 2. CRITICAL: Calc Qty using LIMIT Price (not current)
                    # This ensures Qty * LimitPrice <= WalletBalance
                    qty = usdt_amount / lim_p
                    
                    return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'buy', qty, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                else:
                    # 3. Intelligent Market Buy (Send USDT directly)
                    return EXCHANGE_INSTANCE.create_market_buy_order_with_cost(symbol, usdt_amount)

            try:
                # Attempt 1: Fast (Cached Balance)
                resp = execute_buy(amt_usdt)
            except ccxt.InsufficientFunds:
                # Attempt 2: Failover (Fresh Balance)
                log_retry = " | Retry: Funds"
                fresh = EXCHANGE_INSTANCE.fetch_balance()
                with STATE_LOCK: CACHE['wallet']['USDT'] = fresh['free'].get('USDT', 0.0)
                
                # Recalculate Amount
                amt_usdt = fresh['free'].get('USDT', 0.0) * (0.998 if req_pct >= 99.0 else req_pct / 100.0)
                resp = execute_buy(amt_usdt)

            with STATE_LOCK:
                BOT_STATE[symbol].update({'status': 'HOLDING', 'pending_limit': (otype == 'limit')})

        # ==========================
        #       SELL LOGIC
        # ==========================
        elif side == 'sell':
            # 1. Get Balance
            coin_bal = get_cached_balance(base)
            
            # Double check if 0
            if coin_bal == 0 and not is_cli:
                 f = EXCHANGE_INSTANCE.fetch_balance()
                 coin_bal = f['free'].get(base, 0.0)
            
            if coin_bal == 0:
                with STATE_LOCK: BOT_STATE[symbol].update({'status': 'EMPTY', 'pending_limit': False})
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                LOG_QUEUE.append(('LOG', [ts, symbol, side, "0%", price, "", 0, 0, "Skipped", "Wallet 0", CACHE['wallet'].get('USDT', 0)]))
                return jsonify({"status": "skipped", "msg": "Wallet 0"})

            req_pct = float(data.get('PercentAmount', data.get('percentage', 100)))
            qty = float(data.get('quantity', 0))
            if qty == 0: qty = coin_bal * (req_pct / 100.0)

            def execute_sell(q):
                if otype == 'limit':
                    # Calculate Limit Price
                    cur_p = safe_float(price)
                    if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                    
                    lim_p = float(data.get('limit_price', cur_p))
                    if lim_p == 0: lim_p = cur_p
                    lim_p *= (1 - (slip / 100.0))
                    
                    return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'sell', q, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                else:
                    return EXCHANGE_INSTANCE.create_order(symbol, 'market', 'sell', q)

            try:
                # Attempt 1
                resp = execute_sell(qty)
            except ccxt.InsufficientFunds:
                # Attempt 2: Drift Correction
                log_retry = " | Retry: Drift"
                fresh = EXCHANGE_INSTANCE.fetch_balance()
                real_qty = fresh['free'].get(base, 0.0)
                
                # Update Cache
                with STATE_LOCK: CACHE['wallet'][base] = real_qty
                
                # If we were trying to sell 100%, use the full real balance
                if req_pct >= 99.9: qty = real_qty
                else: qty = real_qty * (req_pct / 100.0)
                
                resp = execute_sell(qty)

            with STATE_LOCK: 
                BOT_STATE[symbol].update({'status': 'EMPTY', 'pending_limit': (otype == 'limit')})

        # ==========================
        #       LOGGING
        # ==========================
        mapped = {
            "symbol": resp.get('symbol'), "side": resp.get('side'), "type": resp.get('type'),
            "status": resp.get('status'), "executedQty": resp.get('filled', 0),
            "cummulativeQuoteQty": resp.get('cost', 0), "price": resp.get('average', resp.get('price', 0))
        }
        
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_pct = data.get('PercentAmount', data.get('percentage', 'Def'))
        final_reason = reason + log_retry
        
        LOG_QUEUE.append(('LOG', [ts, symbol, side, f"{log_pct}%", price, "", mapped['price'], mapped['executedQty'], mapped['status'], final_reason, CACHE['wallet'].get('USDT')]))
        
        return jsonify(mapped)

    except Exception as e:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_pct = data.get('PercentAmount', data.get('percentage', 0))
        full_err = f"{reason} | {str(e)}"
        LOG_QUEUE.append(('LOG', [ts, symbol, side, f"{log_pct}%", price, "", 0, 0, "Error", full_err, CACHE['wallet'].get('USDT', 0)]))
        return jsonify({"status": "error", "msg": str(e), "code": 500})

@app.route('/panic', methods=['POST'])
def panic():
    return special_execute()

@app.route('/cli', methods=['POST'])
def cli():
    ensure_threads_running()
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    m, p = data.get('method'), data.get('params', {})

    if m == "debug_memory": return jsonify({"settings": BOT_SETTINGS, "state": BOT_STATE, "cache": CACHE['wallet'], "ex": CURRENT_EXCHANGE_NAME})
    if m == "get_capital_status":
        bal = CACHE['wallet'].get('USDT', 0.0)
        return jsonify({"dedicated_cap": bal, "reinvest_pct": BOT_SETTINGS['e2_pct'], "wallet_balance": bal, "effective_cap": bal})
    if m == "list_exchanges": return jsonify(list(EXCHANGE_CONFIG.keys()))
    if m == "set_exchange": return jsonify({"status": "success", "msg": f"Switched to {data.get('name')}"}) if load_exchange(data.get('name')) else jsonify({"status": "error"})

    if not EXCHANGE_INSTANCE: return jsonify({"error": "No Exchange"}), 500
    try:
        if m == "account":
            b = EXCHANGE_INSTANCE.fetch_balance()
            out = []
            for k, v in b['total'].items():
                if v > 0: out.append({'asset': k, 'free': b['free'][k], 'locked': b['used'][k]})
            return jsonify({"balances": out})
        if m == "ticker_price": return jsonify({"price": EXCHANGE_INSTANCE.fetch_ticker(normalize_symbol(p.get('symbol')))['last']})
        if m == "get_orders": return jsonify(EXCHANGE_INSTANCE.fetch_orders(normalize_symbol(p.get('symbol'))))
        if m == "get_open_orders": return jsonify(EXCHANGE_INSTANCE.fetch_open_orders(normalize_symbol(p.get('symbol'))))
        if m == "cancel_open_orders": return jsonify(EXCHANGE_INSTANCE.cancel_all_orders(normalize_symbol(p.get('symbol'))))
    except Exception as e: return jsonify({"error": str(e)})
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)