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

client = Spot(
    api_key=API_KEY, 
    api_secret=API_SECRET, 
    base_url=BASE_URL
)

GOOGLE_CLIENT = None

# --- ADVANCED MEMORY BRAIN ---
# 1. Settings (Synced from Google Sheets)
BOT_SETTINGS = {
    "e2_pct": 100.0,
    "f2_type": "MARKET",
    "j2_slip": 0.0
}

# 2. State (Tracks "HOLDING" vs "EMPTY" and "pending_limit")
# Structure: { "BTCUSDT": { "status": "EMPTY", "pending_limit": False } }
BOT_STATE = {}

# 3. Cache (Stores Exchange Info & Balances to avoid API calls)
CACHE = {
    "exchange_info": {},    # Stores stepSize, tickSize per symbol
    "wallet": {"USDT": 0.0} # Stores balances: {"USDT": 1000.0, "BTC": 0.001}
}

# 4. Thread Safety
STATE_LOCK = threading.Lock()

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

def get_cached_step(symbol):
    """Returns step size from RAM (Instant)"""
    return CACHE['exchange_info'].get(symbol, {'step': '0.00001'})['step']

def get_cached_tick(symbol):
    """Returns tick size from RAM (Instant)"""
    return CACHE['exchange_info'].get(symbol, {'tick': '0.000001'})['tick']

def get_state(symbol):
    """Returns or initializes state for a symbol"""
    if symbol not in BOT_STATE:
        BOT_STATE[symbol] = {"status": "EMPTY", "pending_limit": False}
    return BOT_STATE[symbol]

def get_cached_balance(asset):
    """Returns balance from RAM"""
    return CACHE['wallet'].get(asset, 0.0)

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
                    sheet.update(f'A{next_row}:K{next_row}', [data])
                LOG_QUEUE.pop(0)
            except Exception as e:
                print(f"Logger Retrying: {e}")
                time.sleep(5)
        time.sleep(1)

def background_sync_func():
    """Syncs settings, Updates Dashboard, and Refreshes Wallet Cache"""
    time.sleep(5) 
    tick = 0 
    while True:
        try:
            # --- TASK A: Sync Settings from Google (Every 10s) ---
            if tick % 2 == 0: 
                sheet = get_sheet()
                data = sheet.batch_get(['E2', 'G2', 'K2'])
                val_e2 = safe_float(data[0][0][0] if (len(data) > 0 and data[0]) else 100)
                val_f2 = str(data[1][0][0]).upper() if (len(data) > 1 and data[1]) else "MARKET"
                val_j2 = safe_float(str(data[2][0][0]).replace("%", "") if (len(data) > 2 and data[2]) else 0)
                
                with STATE_LOCK:
                    BOT_SETTINGS['e2_pct'] = val_e2
                    BOT_SETTINGS['f2_type'] = val_f2
                    BOT_SETTINGS['j2_slip'] = val_j2

            # --- TASK B: Refresh Wallet Cache (Every 30s) ---
            if tick % 6 == 0:
                try:
                    acct = client.account()
                    with STATE_LOCK:
                        # Reset cache slightly to ensure fresh data
                        for b in acct['balances']:
                            CACHE['wallet'][b['asset']] = float(b['free'])
                except Exception as e:
                    print(f"Wallet Sync Error: {e}")

            # --- TASK C: Update Dashboard (Visuals) ---
            if tick % 6 == 0:
                sheet = get_sheet()
                usdt = CACHE['wallet'].get('USDT', 0)
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.update('A2:B2', [[ts, usdt]])
                
                # Update coin balance on dashboard (H2/I2)
                h1_val = sheet.acell('I1').value
                if h1_val:
                    mon_sym = h1_val.replace("USDT","").strip().upper()
                    c_bal = CACHE['wallet'].get(mon_sym, 0.0)
                    sheet.update('I2', [[c_bal]])

        except Exception as e:
            print(f"Sync Error: {e}")
            time.sleep(10)
        
        tick += 1
        time.sleep(5)

def initialize_runtime():
    """Runs once on startup to load Cache and State."""
    print("--- INITIALIZING BOT STATE ---")
    try:
        # 1. Load Exchange Info (Heavy payload, done once)
        info = client.exchange_info()
        for s in info['symbols']:
            sym = s['symbol']
            step = '0.00001'
            tick = '0.000001'
            for f in s['filters']:
                if f['filterType'] == 'LOT_SIZE': step = f['stepSize']
                if f['filterType'] == 'PRICE_FILTER': tick = f['tickSize']
            CACHE['exchange_info'][sym] = {'step': step, 'tick': tick}
        print(f"Loaded Info for {len(CACHE['exchange_info'])} symbols.")

        # 2. Check Real Balances & Open Orders
        acct = client.account()
        for b in acct['balances']:
            asset = b['asset']
            free = float(b['free'])
            locked = float(b['locked'])
            if free > 0 or locked > 0:
                CACHE['wallet'][asset] = free

        # 3. Check Open Orders to set pending_limit flag
        open_orders = client.get_open_orders()
        # Mark symbols that have open orders
        pending_map = {o['symbol']: True for o in open_orders}
        
        # Initialize BOT_STATE based on open orders
        for sym in pending_map:
            BOT_STATE[sym] = {"status": "HOLDING", "pending_limit": True}
            
        print("Initialization Complete.")
    except Exception as e:
        print(f"Init Failed: {e}")

# Run it immediately
initialize_runtime()

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
    
    # 1. PARSE SYMBOLS
    raw_s = data['symbol'].upper().replace("/", "")
    symbol = raw_s + "T" if raw_s.endswith("USD") and not raw_s.endswith("USDT") else raw_s
    side = data['side'].upper()
    base_asset = symbol.replace("USDT", "")
    
    # 2. READ MEMORY & CHECK DOUBLE ALERTS (INSTANT)
    with STATE_LOCK:
        state = get_state(symbol)
        current_status = state['status']
        pending_limit = state['pending_limit']
        
        e2_pct = BOT_SETTINGS['e2_pct']
        f2_type = BOT_SETTINGS['f2_type']
        j2_slip = BOT_SETTINGS['j2_slip']

    # --- BLOCK: DOUBLE ALERT PROTECTION ---
    if side == 'BUY' and current_status == 'HOLDING':
        reason = "Skipped: Already Holding (Memory)"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG_QUEUE.append(('LOG', [ts, symbol, side, 0, 0, 0, 0, 0, "Skipped", reason, get_cached_balance("USDT")]))
        return jsonify({"status": "skipped", "msg": reason})

    if side == 'SELL' and current_status == 'EMPTY':
        reason = "Skipped: Already Empty (Memory)"
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG_QUEUE.append(('LOG', [ts, symbol, side, 0, 0, 0, 0, 0, "Skipped", reason, get_cached_balance("USDT")]))
        return jsonify({"status": "skipped", "msg": reason})

    # 3. SMART BLIND CANCEL
    # Only cancel if we think we have a limit order pending
    if pending_limit:
        try: client.cancel_open_orders(symbol=symbol)
        except: pass 

    # 4. PREPARE EXECUTION
    sent_price = data.get('price', 'Market')
    payload_type = data.get('type', 'MARKET').upper()
    target_type = payload_type
    
    # Override if Google Settings say LIMIT (unless Manual CLI)
    is_manual_cli = "CLI" in data.get('reason', '')
    if not is_manual_cli and payload_type == 'MARKET' and 'LIMIT' in f2_type:
        if sent_price != 'Market' and safe_float(sent_price) > 0:
            target_type = 'LIMIT'
    
    try:
        resp = {}
        status = "Pending"
        
        # --- BUY LOGIC ---
        if side == 'BUY':
            wallet_usdt = get_cached_balance("USDT")
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2_pct)))
            
            # Logic: Use quoteOrderQty (USDT amount)
            amt_usdt = wallet_usdt * (req_pct / 100.0)
            if req_pct >= 99.9: amt_usdt = wallet_usdt * 0.998 # Buffer for fees
            
            if amt_usdt < 5:
                raise Exception(f"Insufficient USDT: {amt_usdt:.2f}")

            params = {"symbol": symbol, "side": "BUY", "type": target_type}

            if target_type == 'LIMIT':
                # For Limit, we need price + coin quantity
                raw_price = float(data.get('limit_price', sent_price))
                if raw_price == 0: raw_price = get_coin_price(symbol) # Fallback slow call
                
                adj_price = raw_price * (1 + (j2_slip / 100.0))
                tick_size = get_cached_tick(symbol)
                final_lim = round_step_size(adj_price, tick_size)
                
                # Calc Quantity: USDT / Price
                qty_coins = amt_usdt / final_lim
                step = get_cached_step(symbol)
                qty_coins = round_step_size(qty_coins, step)
                
                params['timeInForce'] = data.get('timeInForce', 'GTC')
                params['quantity'] = qty_coins
                params['price'] = "{:.8f}".format(final_lim).rstrip('0').rstrip('.')
            else:
                # INSTANT MARKET BUY
                params['quoteOrderQty'] = round(amt_usdt, 2)

            resp = client.new_order(**params)
            status = "Filled/Open"
            
            # MEMORY UPDATE
            with STATE_LOCK:
                BOT_STATE[symbol]['status'] = "HOLDING"
                BOT_STATE[symbol]['pending_limit'] = (target_type == 'LIMIT')

        # --- SELL LOGIC ---
        elif side == 'SELL':
            coin_bal = get_cached_balance(base_asset)
            if coin_bal == 0:
                 raise Exception("Cache says 0 coins")

            # Logic: Use Coin Quantity from Cache
            explicit_qty = float(data.get('quantity', 0))
            sell_pct = float(data.get('PercentAmount', data.get('percentage', 100)))
            
            if explicit_qty > 0: qty = explicit_qty
            else: qty = coin_bal * (sell_pct / 100.0)

            step = get_cached_step(symbol)
            qty = round_step_size(qty, step) 
            
            params = {"symbol": symbol, "side": "SELL", "type": target_type}

            if target_type == 'LIMIT':
                raw_price = float(data.get('limit_price', sent_price))
                if raw_price == 0: raw_price = get_coin_price(symbol) # Fallback
                
                adj_price = raw_price * (1 - (j2_slip / 100.0))
                tick_size = get_cached_tick(symbol)
                final_lim = round_step_size(adj_price, tick_size)
                
                params['quantity'] = qty
                params['price'] = "{:.8f}".format(final_lim).rstrip('0').rstrip('.')
                params['timeInForce'] = data.get('timeInForce', 'GTC')
            else:
                # INSTANT MARKET SELL
                params['quantity'] = qty
            
            resp = client.new_order(**params)  
            status = "Filled"

            # MEMORY UPDATE
            with STATE_LOCK:
                BOT_STATE[symbol]['status'] = "EMPTY"
                BOT_STATE[symbol]['pending_limit'] = (target_type == 'LIMIT')

        # 5. AGGREGATION & LOGGING
        total_qty = 0.0
        total_quote = 0.0
        exec_price = 0.0
        exec_qty = 0.0
        
        if 'fills' in resp:
            for f in resp['fills']:
                total_qty += float(f['qty'])
                total_quote += float(f['price']) * float(f['qty'])
        
        if total_qty > 0:
            exec_qty = total_qty
            exec_price = total_quote / total_qty
        else:
            exec_qty = float(resp.get('origQty', 0))
            # If Limit, use the price we sent
            if 'price' in params: exec_price = float(params['price'])

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        reason = data.get('reason', '')
        wallet_now = get_cached_balance("USDT")
        
        row = [ts, symbol, side, f"{req_pct if side=='BUY' else sell_pct}%", sent_price, 0, exec_price, exec_qty, status, reason, wallet_now]
        LOG_QUEUE.append(('LOG', row))
        
        return jsonify(resp)

    except Exception as e:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        LOG_QUEUE.append(('LOG', [ts, symbol, "ERROR", 0, 0, 0, 0, 0, str(e), 0, 0]))
        # Revert state on error if needed? 
        # Actually safer to let background thread fix it later, or manually fix via CLI
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    ensure_threads_running()
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    # 1. Debug Memory: Return all 3 memory stores
    if method == "debug_memory":
        return jsonify({
            "settings": BOT_SETTINGS,
            "state": BOT_STATE,
            "cache_wallet": CACHE['wallet']
        })
    
    # 2. Get Capital Status: FIXED to use Cache and Settings
    if method == "get_capital_status":
        try:
            # We use cached values for speed, or you could force a sheet read here if you prefer
            # Let's use the RAM values which are synced every 10s
            e2 = BOT_SETTINGS['e2_pct']
            
            # Use Cached Balance
            bal = get_cached_balance("USDT")
            
            return jsonify({
                "dedicated_cap": bal, 
                "reinvest_pct": e2, 
                "wallet_balance": bal, 
                "effective_cap": bal
            })
        except Exception as e:
             return jsonify({"error": f"Status Check Failed: {str(e)}"}), 500
    
    # 3. Reset State (Manual Fix)
    if method == "reset_state":
        # Params: symbol, status (EMPTY/HOLDING)
        target_sym = params.get('symbol')
        new_status = params.get('status')
        if target_sym and new_status in ['EMPTY', 'HOLDING']:
            with STATE_LOCK:
                if target_sym not in BOT_STATE: BOT_STATE[target_sym] = {}
                BOT_STATE[target_sym]['status'] = new_status
                BOT_STATE[target_sym]['pending_limit'] = False
            return jsonify({"msg": f"State updated for {target_sym} to {new_status}"})
        return jsonify({"error": "Invalid params"}), 400
    
    # 4. Standard Binance Methods
    if hasattr(client, method):
        return jsonify(getattr(client, method)(**params))
    
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)