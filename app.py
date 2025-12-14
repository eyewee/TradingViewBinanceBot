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

# --- IN-MEMORY CACHE (Settings Only) ---
BOT_MEMORY = {
    "e2_pct": 100.0,
    "f2_type": "MARKET",
    "j2_slip": 0.0
}

# --- LOGGING QUEUE ---
LOG_QUEUE = []

# --- THREAD CONTROL ---
THREADS = { "logger": None, "sync": None }

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
        creds = json.loads(GOOGLE_JSON)
        GOOGLE_CLIENT = gspread.service_account_from_dict(creds, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")

def get_balance(asset):
    """Robust balance checker"""
    for attempt in range(3):
        try:
            acct = client.account()
            for a in acct['balances']:
                if a['asset'] == asset: return float(a['free'])
            return 0.0
        except: time.sleep(0.5)
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

# --- WORKER FUNCTIONS ---
def logger_worker_func():
    global LOG_QUEUE
    print("Logger Thread Started")
    while True:
        if len(LOG_QUEUE) > 0:
            task_type, data = LOG_QUEUE[0]
            try:
                sheet = get_sheet()
                # We only log rows now, no state updates
                if task_type == 'LOG':
                    col_a = sheet.col_values(1)
                    next_row = len(col_a) + 1
                    if next_row < 6: next_row = 6
                    sheet.update(f'A{next_row}:J{next_row}', [data])
                LOG_QUEUE.pop(0)
            except Exception as e:
                print(f"Logger Retrying: {e}")
                time.sleep(5)
        time.sleep(1)

def background_sync_func():
    """Syncs settings (E2, F2, J2) and Updates Dashboard"""
    global BOT_MEMORY
    time.sleep(2) 
    tick = 0 
    while True:
        try:
            sheet = get_sheet()
            # Task A: Sync Settings (Ignore D2)
            data = sheet.batch_get(['E2', 'F2', 'J2'])
            
            val_e2 = safe_float(data[0][0][0] if (len(data) > 0 and data[0]) else 100)
            val_f2 = str(data[1][0][0]).upper() if (len(data) > 1 and data[1]) else "MARKET"
            raw_slip = str(data[2][0][0]) if (len(data) > 2 and data[2]) else "0"
            val_j2 = safe_float(raw_slip.replace("%", ""))

            BOT_MEMORY['e2_pct'] = val_e2
            BOT_MEMORY['f2_type'] = val_f2
            BOT_MEMORY['j2_slip'] = val_j2

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

def ensure_threads_running():
    global THREADS
    if THREADS["logger"] is None or not THREADS["logger"].is_alive():
        print("Starting Logger Thread...")
        THREADS["logger"] = threading.Thread(target=logger_worker_func, daemon=True)
        THREADS["logger"].start()
    if THREADS["sync"] is None or not THREADS["sync"].is_alive():
        print("Starting Sync Thread...")
        THREADS["sync"] = threading.Thread(target=background_sync_func, daemon=True)
        THREADS["sync"].start()

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
    wallet_now = 0
    
    # Init sheet for fallback
    try: sheet = get_sheet()
    except: sheet = None

    try:
        # 1. READ SETTINGS
        e2_pct = BOT_MEMORY['e2_pct']
        f2_type = BOT_MEMORY['f2_type']
        j2_slip = BOT_MEMORY['j2_slip']
        
        # 2. CHECK COIN HOLDINGS & CANCEL
        base_asset = symbol.replace("USDT","")
        cancel_all_open_orders(symbol)
        
        # Get fresh balances AFTER cancel
        coin_bal = get_balance(base_asset)
        wallet_usdt = get_balance("USDT")
        
        # 3. ORDER TYPE LOGIC
        payload_type = data.get('type', 'MARKET').upper()
        target_type = payload_type
        is_manual_cli = "CLI" in reason
        
        if not is_manual_cli and payload_type == 'MARKET' and 'LIMIT' in f2_type:
            if sent_price != 'Market' and safe_float(sent_price) > 0:
                target_type = 'LIMIT'

        # 4. BUY LOGIC
        if side == 'BUY':
            # Check Holdings (approx value > 5 USD) to prevent double buy
            coin_val_approx = coin_bal * safe_float(sent_price if sent_price != 'Market' else 0)
            
            if coin_val_approx > 5:
                status = "Skipped: Already Holding"
                resp = {"status": "skipped", "msg": f"Holdings {coin_val_approx:.2f} > 5"}
            else:
                # PURE WALLET MATH: Trade = Wallet * (E2%)
                req_pct = float(data.get('PercentAmount', data.get('percentage', e2_pct)))
                
                # SAFETY BUFFER: If buying 100%, use 99.9% to avoid "Insufficient Balance"
                if req_pct >= 99.9:
                    amt = wallet_usdt * 0.999
                else:
                    amt = wallet_usdt * (req_pct / 100.0)
                
                params = {"symbol": symbol, "side": "BUY", "type": target_type}
                
                if target_type == 'LIMIT':
                    raw_price = float(data.get('limit_price', sent_price))
                    adj_price = raw_price * (1 + (j2_slip / 100.0))
                    
                    tick_size = get_price_tick_size(symbol)
                    final_lim = round_step_size(adj_price, tick_size)
                    
                    qty_coins = amt / final_lim 
                    step = get_symbol_step_size(symbol)
                    qty_coins = round_step_size(qty_coins, step)
                    
                    params['timeInForce'] = data.get('timeInForce', 'GTC')
                    params['quantity'] = qty_coins
                    params['price'] = "{:.8f}".format(final_lim).rstrip('0').rstrip('.')
                    amt = qty_coins * final_lim 
                else:
                    params['quoteOrderQty'] = round(amt, 2)

                if amt > 10:
                    resp = client.new_order(**params)
                    status = "Filled/Open"
                else:
                    status = f"Skipped: Amt {amt:.2f} < 10 (Wallet: {wallet_usdt})"
                    resp = {"status": "skipped", "msg": status}

        # 5. SELL LOGIC
        elif side == 'SELL':
            if coin_bal == 0:
                status = "Skipped: No Coins"
                resp = {"status": "Skipped", "msg": "No coins"}
            else:
                step = get_symbol_step_size(symbol)
                qty = round_step_size(coin_bal, step) 
                
                params = {"symbol": symbol, "side": "SELL", "type": target_type}

                if target_type == 'LIMIT':
                    raw_price = float(data.get('limit_price', sent_price))
                    adj_price = raw_price * (1 - (j2_slip / 100.0))
                    tick_size = get_price_tick_size(symbol)
                    final_lim = round_step_size(adj_price, tick_size)
                    params['quantity'] = qty
                    params['price'] = "{:.8f}".format(final_lim).rstrip('0').rstrip('.')
                    params['timeInForce'] = data.get('timeInForce', 'GTC')
                else:
                    params['quantity'] = qty
                
                resp = client.new_order(**params)  
                status = "Filled"

        # --- AGGREGATION & LOGGING ---
        total_qty = 0.0
        total_quote = 0.0
        if 'fills' in resp:
            for f in resp['fills']:
                total_qty += float(f['qty'])
                total_quote += float(f['price']) * float(f['qty'])
        
        if total_qty > 0:
            exec_qty = total_qty
            exec_price = total_quote / total_qty
        else:
            exec_qty = float(resp.get('origQty', 0))
            if status.startswith("Skipped"):
                exec_price = 0
            else:
                if target_type == 'LIMIT' and 'price' in params:
                    exec_price = float(params['price'])
                else:
                    exec_price = safe_float(sent_price) if sent_price != 'Market' else 0

        # Current Wallet for Log (Visual Reference Only)
        wallet_now = get_balance("USDT")

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        applied_pct = f"{req_pct}%" if side == 'BUY' else "100%"
        
        # New Capital Column now simply shows "Wallet Balance"
        row = [ts, symbol, side, applied_pct, sent_price, exec_price, exec_qty, status, reason, wallet_now]
        LOG_QUEUE.append(('LOG', row))
        
        # H2 Update
        try:
            h1_val = sheet.acell('H1').value
            if h1_val and h1_val.replace("USDT","") in symbol: 
                new_coin_bal = get_balance(symbol.replace("USDT",""))
                sheet.update('H2', [[new_coin_bal]])
        except: pass
        
        return jsonify(resp)

    except Exception as e:
        if sheet:
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
            data = sheet.batch_get(['E2', 'F2', 'J2'])
            
            e2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 100)
            
            BOT_MEMORY['e2_pct'] = e2
            
            bal = get_balance("USDT")
            # Dedicated Cap is now just Wallet Balance
            return jsonify({
                "dedicated_cap": bal, 
                "reinvest_pct": e2, 
                "wallet_balance": bal, 
                "effective_cap": bal
            })
        except Exception as e:
             return jsonify({"error": f"Sync Failed: {str(e)}"}), 500
    
    if hasattr(client, method):
        return jsonify(getattr(client, method)(**params))
    
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)