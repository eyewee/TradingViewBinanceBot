import os
import math
import json
import datetime
import time
import threading
from flask import Flask, request, jsonify
from binance.spot import Spot
from binance.error import ClientError
import gspread

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')
BASE_URL = 'https://testnet.binance.vision'

GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)
GOOGLE_CLIENT = None

# --- IN-MEMORY CACHE ---
BOT_MEMORY = {
    "d2_cap": 0.0,
    "e2_pct": 100.0,
    "f2_type": "MARKET"
}

# --- LOGGING QUEUE ---
LOG_QUEUE = []

# --- THREAD CONTROL ---
THREADS = {
    "logger": None,
    "sync": None
}

# --- HELPERS ---
def safe_float(value, default=0.0):
    try:
        if isinstance(value, str):
            clean = value.replace('$', '').replace(',', '').replace(' ', '').strip()
            if clean == "": return default
            return float(clean)
        return float(value)
    except: return default

def get_sheet():
    global GOOGLE_CLIENT
    try:
        if GOOGLE_CLIENT is None:
            creds = json.loads(GOOGLE_JSON)
            GOOGLE_CLIENT = gspread.service_account_from_dict(creds, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")
    except Exception:
        # Force Reconnect
        creds = json.loads(GOOGLE_JSON)
        GOOGLE_CLIENT = gspread.service_account_from_dict(creds, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")

def get_balance(asset):
    try:
        acct = client.account()
        for a in acct['balances']:
            if a['asset'] == asset: return float(a['free'])
    except: pass
    return 0.0

def cancel_all_open_orders(symbol):
    try:
        open_orders = client.get_open_orders(symbol)
        if open_orders:
            client.cancel_open_orders(symbol)
            return True
    except: pass
    return False

def get_coin_price(symbol):
    try:
        return float(client.ticker_price(symbol=symbol)['price'])
    except: return 0.0

def get_symbol_step_size(symbol):
    try:
        info = client.exchange_info(symbol=symbol)
        for f in info['symbols'][0]['filters']:
            if f['filterType'] == 'LOT_SIZE': return f['stepSize']
    except: pass
    return '0.00001'

def get_price_tick_size(symbol):
    try:
        info = client.exchange_info(symbol=symbol)
        for f in info['symbols'][0]['filters']:
            if f['filterType'] == 'PRICE_FILTER': return f['tickSize']
    except: pass
    return '0.000001'

def round_step_size(quantity, step_size):
    precision = int(round(-math.log(float(step_size), 10), 0))
    return float(round(quantity - (quantity % float(step_size)), precision))

def get_cost_basis_from_history(symbol, qty_held):
    if qty_held <= 0: return 0.0
    try:
        trades = client.my_trades(symbol=symbol, limit=20)
        trades.reverse()
        cost_accumulated = 0.0
        qty_needed = qty_held
        for t in trades:
            if t['isBuyer']:
                trade_qty = float(t['qty'])
                trade_quote = float(t['quoteQty'])
                if qty_needed <= 0: break
                if trade_qty >= qty_needed:
                    fraction = qty_needed / trade_qty
                    cost_accumulated += (trade_quote * fraction)
                    qty_needed = 0
                else:
                    cost_accumulated += trade_quote
                    qty_needed -= trade_qty
        return cost_accumulated
    except: return 0.0

def update_compounding_after_trade(sheet, side, usdt_value, coin_qty, symbol):
    global BOT_MEMORY
    
    current_cap = BOT_MEMORY['d2_cap']
    new_d2 = current_cap

    if side == "SELL":
        cost_basis = get_cost_basis_from_history(symbol, coin_qty)
        profit = usdt_value - cost_basis
        new_d2 = current_cap + profit

    BOT_MEMORY['d2_cap'] = new_d2
    
    # CRITICAL DATA: Sync Save (Retries 3 times)
    for attempt in range(3):
        try:
            sheet.batch_update([
                {'range': 'D2', 'values': [[new_d2]]},
                {'range': 'H3', 'values': [[new_d2]]}
            ])
            break
        except Exception:
            time.sleep(1)
    
    return new_d2

# --- WORKER FUNCTIONS ---
def logger_worker_func():
    """Consumes the LOG_QUEUE and writes to Google Sheets"""
    global LOG_QUEUE
    print("Logger Thread Started")
    while True:
        if len(LOG_QUEUE) > 0:
            task_type, data = LOG_QUEUE[0]
            try:
                sheet = get_sheet()
                if task_type == 'LOG':
                    col_a = sheet.col_values(1)
                    next_row = len(col_a) + 1
                    if next_row < 6: next_row = 6
                    sheet.update(f'A{next_row}:J{next_row}', [data])
                    print(f"Log persisted. Queue size: {len(LOG_QUEUE)-1}")
                LOG_QUEUE.pop(0)
            except Exception as e:
                print(f"Logger Retrying: {e}")
                time.sleep(5)
        time.sleep(1)

def background_sync_func():
    """Syncs settings from Sheet to Memory every 15s"""
    global BOT_MEMORY
    time.sleep(2) 
    tick = 0 
    while True:
        try:
            sheet = get_sheet()
            # Task A: Sync Settings
            data = sheet.batch_get(['D2', 'E2', 'F2'])
            
            # Safe unpack
            val_d2 = data[0][0][0] if (len(data) > 0 and data[0]) else 0
            val_e2 = data[1][0][0] if (len(data) > 1 and data[1]) else 100
            val_f2 = data[2][0][0] if (len(data) > 2 and data[2]) else "MARKET"

            BOT_MEMORY['d2_cap'] = safe_float(val_d2)
            BOT_MEMORY['e2_pct'] = safe_float(val_e2)
            BOT_MEMORY['f2_type'] = str(val_f2).upper()

            # Task B: Dashboard
            if tick % 4 == 0: 
                try:
                    usdt = get_balance("USDT")
                    btc = get_coin_price("BTCUSDT")
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sheet.update('A2:C2', [[ts, usdt, btc]])
                    
                    h1_val = sheet.acell('H1').value
                    if h1_val:
                        mon_sym = h1_val.replace("USDT","").strip().upper()
                        c_bal = get_balance(mon_sym)
                        sheet.update('H2', [[c_bal]])
                except: pass
        except Exception as e:
            print(f"Sync Error: {e}")
            time.sleep(60)
        
        tick += 1
        time.sleep(15)

# --- THREAD MANAGER (Gunicorn Fix) ---
def ensure_threads_running():
    global THREADS
    
    # Check Logger
    if THREADS["logger"] is None or not THREADS["logger"].is_alive():
        print("Starting Logger Thread...")
        THREADS["logger"] = threading.Thread(target=logger_worker_func, daemon=True)
        THREADS["logger"].start()
        
    # Check Sync
    if THREADS["sync"] is None or not THREADS["sync"].is_alive():
        print("Starting Sync Thread...")
        THREADS["sync"] = threading.Thread(target=background_sync_func, daemon=True)
        THREADS["sync"].start()

# Check on load
ensure_threads_running()

# --- ROUTES ---
@app.route('/')
def home():
    ensure_threads_running() 
    return "Bot is awake.", 200

@app.route('/webhook', methods=['POST'])
def webhook():
    ensure_threads_running()
    
    data = request.get_json(force=True)
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    raw_s = data['symbol'].upper().replace("/", "")
    symbol = raw_s + "T" if raw_s.endswith("USD") and not raw_s.endswith("USDT") else raw_s
    side = data['side'].upper()
    sent_price = data.get('price', 'Market')
    reason = data.get('reason', '') 
    
    status = "Pending"
    exec_price = 0
    exec_qty = 0
    usdt_value = 0
    final_cap = 0
    
    # Init sheet for fallback logic
    try: sheet = get_sheet()
    except: sheet = None

    try:
        d2_cap = BOT_MEMORY['d2_cap']
        e2_pct = BOT_MEMORY['e2_pct']
        f2_type = BOT_MEMORY['f2_type']
        
        cancel_all_open_orders(symbol)
        
        # Fallback if memory 0
        if d2_cap == 0:
            try:
                if sheet:
                    s_data = sheet.batch_get(['D2', 'E2', 'F2'])
                    d2_cap = safe_float(s_data[0][0][0] if (len(s_data)>0 and s_data[0]) else 0)
                    e2_pct = safe_float(s_data[1][0][0] if (len(s_data)>1 and s_data[1]) else 100)
                    f2_type = str(s_data[2][0][0]).upper() if (len(s_data)>2 and s_data[2]) else "MARKET"
                    BOT_MEMORY['d2_cap'] = d2_cap
                    BOT_MEMORY['e2_pct'] = e2_pct
                    BOT_MEMORY['f2_type'] = f2_type
            except: pass
        
        final_cap = d2_cap
        
        payload_type = data.get('type', 'MARKET').upper()
        target_type = payload_type
        is_manual_cli = "CLI" in reason
        
        if not is_manual_cli and payload_type == 'MARKET' and 'LIMIT' in f2_type:
            if sent_price != 'Market' and safe_float(sent_price) > 0:
                target_type = 'LIMIT'

        if side == 'BUY':
            bal = get_balance("USDT")
            eff_cap = min(d2_cap, bal)
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2_pct)))
            amt = eff_cap * (req_pct / 100.0)
            
            params = {"symbol": symbol, "side": "BUY", "type": target_type}
            
            if target_type == 'LIMIT':
                raw_price = float(data.get('limit_price', sent_price))
                tick_size = get_price_tick_size(symbol)
                final_limit_price = round_step_size(raw_price, tick_size)
                
                qty_coins = amt / final_limit_price 
                step = get_symbol_step_size(symbol)
                qty_coins = round_step_size(qty_coins, step)
                
                params['timeInForce'] = data.get('timeInForce', 'GTC')
                params['quantity'] = qty_coins
                params['price'] = "{:.8f}".format(final_limit_price).rstrip('0').rstrip('.')
                amt = qty_coins * final_limit_price 
            else:
                params['quoteOrderQty'] = round(amt, 2)

            if amt > 10:
                resp = client.new_order(**params)
                status = "Filled/Open"
            else:
                status = f"Skipped: Amt {amt:.2f} < 10 (Cap: {d2_cap}, Pct: {req_pct})"
                resp = {"status": "skipped", "msg": status}

        elif side == 'SELL':
            base = symbol.replace("USDT","")
            coin_bal = get_balance(base)
            
            if coin_bal == 0:
                status = "Skipped: No Coins"
                resp = {"status": "Skipped", "msg": "No coins to sell"}
            else:
                step = get_symbol_step_size(symbol)
                qty = round_step_size(coin_bal, step) 
                
                params = {"symbol": symbol, "side": "SELL", "type": target_type}

                if target_type == 'LIMIT':
                    raw_price = float(data.get('limit_price', sent_price))
                    tick_size = get_price_tick_size(symbol)
                    final_limit_price = round_step_size(raw_price, tick_size)
                    params['quantity'] = qty
                    params['price'] = "{:.8f}".format(final_limit_price).rstrip('0').rstrip('.')
                    params['timeInForce'] = data.get('timeInForce', 'GTC')
                else:
                    params['quantity'] = qty
                
                resp = client.new_order(**params)  
                status = "Filled"

        if 'cummulativeQuoteQty' in resp:
            usdt_value = float(resp['cummulativeQuoteQty'])
        elif 'origQty' in resp and 'price' in resp:
            usdt_value = float(resp['origQty']) * float(resp['price'])

        if 'fills' in resp and len(resp['fills']) > 0:
            exec_price = float(resp['fills'][0]['price'])
            exec_qty = float(resp['fills'][0]['qty'])
        else:
            if status.startswith("Skipped"):
                exec_price = 0
            else:
                if target_type == 'LIMIT' and 'price' in params:
                    exec_price = float(params['price'])
                else:
                    exec_price = safe_float(sent_price) if sent_price != 'Market' else 0
            exec_qty = float(resp.get('origQty', 0))

        if status.startswith("Filled"):
            final_cap = update_compounding_after_trade(sheet, side, usdt_value, float(exec_qty), symbol)

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        applied_pct = f"{req_pct}%" if side == 'BUY' else "100%"
        
        row = [ts, symbol, side, applied_pct, sent_price, exec_price, exec_qty, status, reason, final_cap]
        
        # QUEUE LOG (Instant Return)
        LOG_QUEUE.append(('LOG', row))
        
        # H2 Update (Instant if possible)
        try:
            h1_val = sheet.acell('H1').value
            if h1_val and h1_val.replace("USDT","") in symbol: 
                new_coin_bal = get_balance(symbol.replace("USDT",""))
                sheet.update('H2', [[new_coin_bal]])
        except: pass
        
        return jsonify(resp)

    except Exception as e:
        # QUEUE ERROR
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        err_row = [ts, symbol, "ERROR", 0, 0, 0, 0, str(e), 0, 0]
        LOG_QUEUE.append(('LOG', err_row))
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    ensure_threads_running()
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    if method == "debug_memory":
        return jsonify(BOT_MEMORY)
    
    if method == "get_capital_status":
        try:
            sheet = get_sheet()
            data = sheet.batch_get(['D2', 'E2', 'F2'])
            d2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 0)
            e2 = safe_float(data[1][0][0] if (len(data)>1 and data[1]) else 100)
            BOT_MEMORY['d2_cap'] = d2
            BOT_MEMORY['e2_pct'] = e2
            bal = get_balance("USDT")
            return jsonify({"dedicated_cap": d2, "reinvest_pct": e2, "wallet_balance": bal, "effective_cap": min(d2, bal)})
        except Exception as e:
             return jsonify({"error": f"Sync Failed: {str(e)}"}), 500
    
    if hasattr(client, method):
        return jsonify(getattr(client, method)(**params))
    
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)