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

PENDING_LOG_UPDATES = {}   # order_id -> {"symbol":..., "row": int, "side":..., "status":...}
ORDER_ROW_LOCK = threading.Lock()

def load_exchange(config_name):
    global EXCHANGE_INSTANCE, CURRENT_EXCHANGE_NAME
    if config_name not in EXCHANGE_CONFIG: return False
    conf = EXCHANGE_CONFIG[config_name]
    try:
        ex_class = getattr(ccxt, conf.get('exchange_id', 'binance'))
        params = {
            'apiKey': conf.get('apiKey'),
            'secret': conf.get('secret'),
            'options': {
                'defaultType': 'spot',
                # --- ENABLE GLOBAL CANCEL ---
                'warnOnFetchOpenOrdersWithoutSymbol': False 
            }
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
        BOT_STATE[symbol] = {
            "status": "EMPTY",          # EMPTY | HOLDING | PENDING
            "pending_limit": False,
            "pending_order_id": None,   # exchange order id we’re waiting on
            "pending_side": None        # "buy" or "sell"
        }
    else:
        # Upgrade older dicts safely
        BOT_STATE[symbol].setdefault("status", "EMPTY")
        BOT_STATE[symbol].setdefault("pending_limit", False)
        BOT_STATE[symbol].setdefault("pending_order_id", None)
        BOT_STATE[symbol].setdefault("pending_side", None)
    return BOT_STATE[symbol]

def logger_worker_func():
    global LOG_QUEUE
    print("Logger Thread Started")
    while True:
         # --- MEMORY SAFETY (if google is down, don't spin eternally ---
        if len(LOG_QUEUE) > 200:
            LOG_QUEUE = LOG_QUEUE[-200:] # Keep only last 200 items
            
        if len(LOG_QUEUE) > 0:
            task = LOG_QUEUE[0]
            try:
                sheet = get_sheet()

                # Backward compatibility: old tuples ('LOG', data)
                if isinstance(task, tuple) and len(task) == 2:
                    task_type, data = task
                    meta = {}
                else:
                    # New format: ('LOG', row_data, meta_dict)
                    task_type = task[0]
                    data = task[1]
                    meta = task[2] if len(task) > 2 else {}

                if task_type == 'LOG':
                    col_a = sheet.col_values(1)
                    nr = max(len(col_a) + 1, 6)
                    sheet.update(f'A{nr}:K{nr}', [data])

                    # If this is an OPEN order, remember which sheet row belongs to which exchange order id
                    order_id = meta.get("order_id")
                    initial_status = str(meta.get("status", "")).lower()
                    if order_id and initial_status in ("open", "new", "partially_filled", "partiallyfilled"):
                        with ORDER_ROW_LOCK:
                            PENDING_LOG_UPDATES[str(order_id)] = {
                                "symbol": meta.get("symbol"),
                                "row": nr,
                                "side": str(meta.get("side", "")).lower(),
                                "status": initial_status
                            }

                elif task_type == 'UPDATE_ROW':
                    # data = {"row":int, "exec_price":..., "exec_qty":..., "status":..., "capital":...}
                    row = int(data["row"])

                    # G=Exec price, H=Exec qty, I=Status, K=Capital
                    # We can update non-adjacent ranges in 2 calls
                    sheet.update(f'G{row}:I{row}', [[
                        data.get("exec_price", 0),
                        data.get("exec_qty", 0),
                        data.get("status", "")
                    ]])
                    sheet.update(f'K{row}', [[data.get("capital", 0)]])

                LOG_QUEUE.pop(0)

            except Exception:
                time.sleep(5)

        time.sleep(1)

def poll_pending_order_rows():
    """
    Checks tracked OPEN orders and updates their existing sheet row when status changes
    (especially when they become CLOSED and exec qty/price become available).
    """
    if not EXCHANGE_INSTANCE:
        return

    with ORDER_ROW_LOCK:
        pending_items = list(PENDING_LOG_UPDATES.items())

    if not pending_items:
        return

    for order_id, info in pending_items:
        symbol = info.get("symbol")
        row = info.get("row")
        side = info.get("side", "")

        if not symbol or not row:
            continue

        try:
            # fetch_order is supported by most ccxt exchanges (including Binance spot)
            o = EXCHANGE_INSTANCE.fetch_order(order_id, symbol)
            if not o:
                continue

            new_status = str(o.get("status", "")).lower()
            # Ignore if still open
            if new_status in ("open", "new"):
                continue

            # Refresh wallet so K gets the *real* post-fill USDT
            bal = refresh_wallet_cache_for_symbol(symbol.split('/')[0] if '/' in symbol else symbol.replace('USDT', ''))
            usdt_now = 0.0
            if bal:
                usdt_now = float(bal['free'].get('USDT', 0.0))
            else:
                usdt_now = get_cached_balance("USDT")

            exec_price = o.get("average", o.get("price", 0)) or 0
            exec_qty = o.get("filled", 0) or 0

            LOG_QUEUE.append((
                'UPDATE_ROW',
                {
                    "row": row,
                    "exec_price": exec_price,
                    "exec_qty": exec_qty,
                    "status": o.get("status", ""),
                    "capital": usdt_now
                }
            ))

            # Update in-memory state based on final status
            if new_status == "closed":
                with STATE_LOCK:
                    if symbol not in BOT_STATE:
                        BOT_STATE[symbol] = {"status": "EMPTY", "pending_limit": False}
                    if side == "buy":
                        BOT_STATE[symbol]["status"] = "HOLDING"
                    elif side == "sell":
                        BOT_STATE[symbol]["status"] = "EMPTY"
                    BOT_STATE[symbol]["pending_limit"] = False

            elif new_status in ("canceled", "cancelled", "rejected", "expired"):
                with STATE_LOCK:
                    if symbol in BOT_STATE:
                        BOT_STATE[symbol]["pending_limit"] = False

            # Done tracking this row
            with ORDER_ROW_LOCK:
                PENDING_LOG_UPDATES.pop(order_id, None)

        except Exception as e:
            # Safe to ignore transient errors; background loop will retry
            print(f"Pending Order Poll Error [{order_id}]: {e}")

def poll_pending_order_state():
    """
    Single source of truth for HOLDING/EMPTY:
    - If pending_order_id exists: check exchange order status.
    - When order closes: BUY => HOLDING, SELL => EMPTY.
    Balances never set status.
    """
    if not EXCHANGE_INSTANCE:
        return

    with STATE_LOCK:
        pending_list = [
            (sym, st.get("pending_order_id"), st.get("pending_side"))
            for sym, st in BOT_STATE.items()
            if st.get("pending_order_id")
        ]

    for sym, oid, pside in pending_list:
        if not oid or not pside:
            continue
        try:
            o = EXCHANGE_INSTANCE.fetch_order(oid, sym)
            if not o:
                continue

            st = str(o.get("status", "")).lower()

            # Still open -> stay pending
            if st in ("open", "new"):
                continue

            # Terminal -> clear pending
            with STATE_LOCK:
                s = get_state(sym)
                s["pending_order_id"] = None
                s["pending_side"] = None
                s["pending_limit"] = False

                if st == "closed":
                    s["status"] = "HOLDING" if pside == "buy" else "EMPTY"
                # canceled/rejected/expired: keep previous status, just not pending

        except Exception as e:
            print(f"Pending State Poll Error [{sym} {oid}]: {e}")

def background_sync_func():
    time.sleep(2)
    tick = 0
    
    while True:
        try:
            if not EXCHANGE_INSTANCE: 
                time.sleep(1)
                continue
                
            sheet = None
            # Fetch sheet every 10 seconds
            if tick % 5 == 0:
                try: sheet = get_sheet()
                except: pass

            # 1. SYNC SETTINGS (Every 6 seconds)
            if tick % 3 == 0 and sheet:
                try:
                    # E2=%, G2=Type, K1=Slip, H2=Symbol (Fixed from H1)
                    data = sheet.batch_get(['E2', 'G2', 'K1', 'H2'])
                    val_e2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 100)
                    val_f2 = str(data[1][0][0]).upper() if (len(data)>1 and data[1]) else "MARKET"
                    val_j2 = safe_float(str(data[2][0][0]).replace("%", "") if (len(data)>2 and data[2]) else 0)
                    val_h2 = str(data[3][0][0]).strip().upper() if (len(data)>3 and data[3]) else ""
                    
                    with STATE_LOCK:
                        BOT_SETTINGS.update({
                            'e2_pct': val_e2, 
                            'f2_type': val_f2, 
                            'j2_slip': val_j2, 
                            'active_symbol': val_h2
                        })
                except Exception as e: print(f"Settings Sync Error: {e}")

            # 2. SYNC WALLET (Every 2 seconds)
            # FIXED: Removed 'local_ex' check. Now uses EXCHANGE_INSTANCE directly.
            if tick % 1 == 0:
                try:
                    bal = EXCHANGE_INSTANCE.fetch_balance()
                    
                    with STATE_LOCK:
                        # Clear and update USDT balance
                        CACHE['wallet'] = {'USDT': float(bal['free'].get('USDT', 0.0))}
                        
                        # Update other coin balances
                        for a, amt in bal['free'].items():
                            if amt > 0 and a != 'USDT': CACHE['wallet'][a] = float(amt)                        
                        
                except Exception as e: print(f"Wallet Sync Error: {e}")         
                   
            # 2.5. POLL OPEN LIMIT ORDERS AND UPDATE THEIR LOG ROWS
            if tick % 1 == 0:
                try:
                    poll_pending_order_rows()
                except Exception as e:
                    print(f"Pending Poll Loop Error: {e}")  
                    
            # 2.6. RESOLVE ORDER-TRUTH STATE (PENDING -> HOLDING/EMPTY)
            if tick % 1 == 0:
                try:
                    poll_pending_order_state()
                except Exception as e:
                    print(f"Pending State Loop Error: {e}")

            # 3. UPDATE DASHBOARD (Every 10 seconds)
            if tick % 5 == 0 and sheet:
                try:
                    # Use a local copy to avoid dictionary size change errors during loop
                    with STATE_LOCK:
                        current_wallet = CACHE['wallet'].copy()
                        active_sym = BOT_SETTINGS.get('active_symbol', '').upper()

                    usdt_val = current_wallet.get('USDT', 0.0)
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    
                    # Update A2 (Time) and B2 (USDT Balance)
                    sheet.update('A2:B2', [[ts, usdt_val]])
                    
                    # --- IMPROVED I2 LOGIC ---
                    # Extract Asset Name (e.g., "PUMP/USDT" -> "PUMP")
                    if active_sym:
                        # Strip common suffixes and slashes
                        asset_name = active_sym.replace("/USDT", "").replace("USDT", "").replace("/", "").strip()
                        
                        # Look up in wallet using the cleaned Asset Name
                        coin_bal = current_wallet.get(asset_name, 0.0)
                        
                        # Update I2
                        sheet.update('I2', [[coin_bal]])
                except Exception as e: print(f"Dashboard Update Error: {e}")              

        except Exception as e:
            print(f"Main Loop Error: {e}")
            time.sleep(5)
            
        tick += 1
        time.sleep(2)

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
    
    # 1. UNLOCK PHASE
    log = brute_force_cancel(symbol)
    
    try:
        # 2. FETCH FRESH BALANCE
        bal = EXCHANGE_INSTANCE.fetch_balance()
        free_qty = bal['free'].get(base, 0.0)
        locked_qty = bal['used'].get(base, 0.0)
        
        log.append(f"State: Free={free_qty} | Locked={locked_qty}")
        
        resp = "Nothing to sell"
        
        # 3. SELL ONLY FREE FUNDS
        if free_qty > 0:
            # Use 'create_market_sell_order' explicitly
            res = EXCHANGE_INSTANCE.create_order(symbol, 'market', 'sell', free_qty)
            log.append(f"PANIC SOLD {free_qty} {base}")
            resp = str(res.get('id', 'Filled'))
            
            # Reset State
            with STATE_LOCK:
                BOT_STATE[symbol] = {'status': 'EMPTY', 'pending_limit': False}
                CACHE['wallet'][base] = 0.0
                # Force immediate background sync update
                CACHE['wallet']['USDT'] = float(bal['free'].get('USDT', 0.0))

        elif locked_qty > 0:
            log.append("CRITICAL: Funds remained locked after cancel. Manual intervention on Binance required.")
            
        return jsonify({"status": "Done", "log": log, "order": resp})
    except Exception as e:
        return jsonify({"status": "Error", "log": log + [str(e)]})

def refresh_wallet_cache_for_symbol(base_asset=None):
    """
    Pull fresh exchange balance and update CACHE immediately.
    Returns fresh balance dict or None.
    """
    try:
        bal = EXCHANGE_INSTANCE.fetch_balance()
        with STATE_LOCK:
            CACHE['wallet']['USDT'] = float(bal['free'].get('USDT', 0.0))
            if base_asset:
                CACHE['wallet'][base_asset] = float(bal['free'].get(base_asset, 0.0))
        return bal
    except Exception as e:
        print(f"Immediate Wallet Refresh Error: {e}")
        return None

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
    reason = data.get('reason', 'Signal') 

   # Initialize calc_p in the outer scope
    calc_p = price 
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
        
        # State Check (Memory)
        st = get_state(symbol)
        if not is_cli and side == 'buy' and st['status'] in ('HOLDING', 'PENDING'):
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            skip_msg = f"{reason} | Skipped: Already Holding or Pending"
            LOG_QUEUE.append(('LOG', [ts, symbol, side, "0%", price, "", 0, 0, "Skipped", skip_msg, CACHE['wallet'].get('USDT', 0)]))
            return jsonify({"status": "skipped", "msg": skip_msg})
        
        # Pre-Trade Cleanup (Memory)
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
        log_price = price

        # --- BULLETPROOF WRAPPER ---
        def safe_execute(action_func, is_buy):
            nonlocal log_retry, calc_p
            try:
                # Attempt 1: Fast (Memory)
                return action_func() 
            except Exception as e:
                # Attempt 2: Bulletproof Retry
                # If ANY fund/state error occurs -> Force Clean & Retry
                err_str = str(e).lower()
                if "insufficient" in err_str or "balance" in err_str or "account" in err_str or "margin" in err_str:
                    log_retry = " | Retry: Cleanup & Funds"
                    
                    # A. Force Cancel (Clear stuck orders)
                    try: EXCHANGE_INSTANCE.cancel_all_orders(symbol)
                    except: pass
                    
                    # B. Force Sync
                    fresh = EXCHANGE_INSTANCE.fetch_balance()
                    with STATE_LOCK:
                        CACHE['wallet']['USDT'] = fresh['free'].get('USDT', 0.0)
                        CACHE['wallet'][base] = fresh['free'].get(base, 0.0)
                    
                    # C. Recalculate with fresh balance
                    if is_buy:
                        # Recalc Buy Amount
                        req_pct = float(data.get('PercentAmount', data.get('percentage', e2)))
                        fresh_amt = CACHE['wallet']['USDT'] * (0.998 if req_pct >= 99.0 else req_pct / 100.0)
                        
                        if otype == 'limit':
                            cur_p = safe_float(price)
                            if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                            lim_p = float(data.get('limit_price', cur_p))
                            if lim_p == 0: lim_p = cur_p
                            lim_p *= (1 + (slip / 100.0))
                            calc_p = lim_p
                            qty = fresh_amt / lim_p
                            return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'buy', qty, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                        else:
                            return EXCHANGE_INSTANCE.create_market_buy_order_with_cost(symbol, fresh_amt)
                    else:
                        # Recalc Sell Qty
                        fresh_qty = CACHE['wallet'][base]
                        req_pct = float(data.get('PercentAmount', data.get('percentage', 100)))
                        q = fresh_qty if req_pct >= 99.9 else fresh_qty * (req_pct / 100.0)
                        
                        if otype == 'limit':
                            cur_p = safe_float(price)
                            if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                            lim_p = float(data.get('limit_price', cur_p))
                            if lim_p == 0: lim_p = cur_p
                            lim_p *= (1 - (slip / 100.0))
                            calc_p = lim_p  # Store the calculated price
                            return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'sell', q, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                        else:
                            return EXCHANGE_INSTANCE.create_order(symbol, 'market', 'sell', q)
                raise e

        # ==========================
        #       BUY LOGIC
        # ==========================
        if side == 'buy':
            wallet_usdt = get_cached_balance("USDT")
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2)))
            amt_usdt = wallet_usdt * (0.998 if req_pct >= 99.0 else req_pct / 100.0)

            def run_buy():
                nonlocal calc_p, log_price
                if amt_usdt < 5: raise Exception(f"Insufficient USDT: {amt_usdt:.2f}")

                if otype == 'limit':
                    cur_p = safe_float(price)
                    if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                    lim_p = float(data.get('limit_price', cur_p))
                    if lim_p == 0: lim_p = cur_p
                    lim_p *= (1 + (slip / 100.0))
                    calc_p = lim_p  # Store the calculated price
                    log_price = lim_p
                    qty = amt_usdt / lim_p
                    return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'buy', qty, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                else:
                    return EXCHANGE_INSTANCE.create_market_buy_order_with_cost(symbol, amt_usdt)

            resp = safe_execute(run_buy, is_buy=True)

            # Immediate wallet refresh so K column logs current USDT (especially for MARKET buys)
            refresh_wallet_cache_for_symbol(base)

            with STATE_LOCK:
                st = get_state(symbol)
                order_status = str(resp.get("status", "")).lower()
                order_id = str(resp.get("id", "")) if resp else None

                if order_status in ("open", "new") or otype == "limit":
                    st["status"] = "PENDING"
                    st["pending_limit"] = True
                    st["pending_order_id"] = order_id
                    st["pending_side"] = "buy"
                else:
                    # market order usually closed immediately
                    st["status"] = "HOLDING" if order_status == "closed" else st["status"]
                    st["pending_limit"] = False
                    st["pending_order_id"] = None
                    st["pending_side"] = None

        # ==========================
        #       SELL LOGIC
        # ==========================
        elif side == 'sell':
            # 1. Get Balance
            coin_bal = get_cached_balance(base)
            
            # Double check if 0 (Failover)
            if coin_bal == 0 and not is_cli:
                 f = EXCHANGE_INSTANCE.fetch_balance()
                 coin_bal = f['free'].get(base, 0.0)
            
            if coin_bal == 0:
                # REPLACEMENT LOGIC:
                # 1. Use Brute Force Cancel
                cancel_logs = brute_force_cancel(symbol)
                cancel_msg = " | Cancelled Orders" if cancel_logs else ""
                
                # 2. Check if coins were unlocked
                f = EXCHANGE_INSTANCE.fetch_balance()
                new_free = f['free'].get(base, 0.0)
                
                if new_free > 0:
                    # If coins appeared after cancel, DON'T SKIP. 
                    # Continue to the execution part below.
                    coin_bal = new_free
                else:
                    # Truly 0. Empty.
                    with STATE_LOCK: BOT_STATE[symbol].update({'status': 'EMPTY', 'pending_limit': False})
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    skip_msg = f"{reason} | Skipped: Wallet 0{cancel_msg}"
                    LOG_QUEUE.append(('LOG', [ts, symbol, side, "0%", price, "", 0, 0, "Skipped", skip_msg, CACHE['wallet'].get('USDT', 0)]))
                    return jsonify({"status": "skipped", "msg": skip_msg})

            req_pct = float(data.get('PercentAmount', data.get('percentage', 100)))
            qty = float(data.get('quantity', 0))
            if qty == 0: qty = coin_bal * (req_pct / 100.0)

            def run_sell():
                nonlocal calc_p, log_price
                if otype == 'limit':
                    cur_p = safe_float(price)
                    if cur_p == 0: cur_p = float(EXCHANGE_INSTANCE.fetch_ticker(symbol)['last'])
                    lim_p = float(data.get('limit_price', cur_p))
                    if lim_p == 0: lim_p = cur_p
                    lim_p *= (1 - (slip / 100.0))
                    calc_p = lim_p  # Store the calculated price
                    log_price = lim_p
                    return EXCHANGE_INSTANCE.create_order(symbol, 'limit', 'sell', qty, lim_p, {'timeInForce': data.get('timeInForce', 'GTC')})
                else:
                    return EXCHANGE_INSTANCE.create_order(symbol, 'market', 'sell', qty)

            resp = safe_execute(run_sell, is_buy=False)

            # Immediate wallet refresh so K column logs post-sell USDT (fixes stale K bug)
            refresh_wallet_cache_for_symbol(base)

            with STATE_LOCK:
                st = get_state(symbol)
                order_status = str(resp.get("status", "")).lower()
                order_id = str(resp.get("id", "")) if resp else None

                if order_status in ("open", "new") or otype == "limit":
                    st["status"] = "PENDING"
                    st["pending_limit"] = True
                    st["pending_order_id"] = order_id
                    st["pending_side"] = "sell"
                else:
                    st["status"] = "EMPTY" if order_status == "closed" else st["status"]
                    st["pending_limit"] = False
                    st["pending_order_id"] = None
                    st["pending_side"] = None

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
        final_reason = f"{reason}{log_retry}"
        
        # Correct Column Mapping:
        # Col E (Sent/Received): price (Market or original JSON price)
        # Col F (Calculated): calc_p (Price adjusted with slip %)
        # Col G (Exec): mapped['price'] (Actual fill price)
        row_data = [
            ts, symbol, side, f"{log_pct}%", price, calc_p,
            mapped['price'], mapped['executedQty'], mapped['status'], final_reason,
            CACHE['wallet'].get('USDT')
        ]

        LOG_QUEUE.append((
            'LOG',
            row_data,
            {
                "order_id": str(resp.get('id', '')) if resp else '',
                "symbol": resp.get('symbol', symbol),
                "side": side,
                "status": mapped.get('status', '')
            }
        ))
        
        return jsonify(mapped)

    except Exception as e:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        full_err = f"{reason} | {str(e)}"
        # Error Log: Use 'price' (Signal Price) since we didn't execute
        # Ensure error logs also follow the new column order
        LOG_QUEUE.append(('LOG', [ts, symbol, side, f"{log_pct}%", price, calc_p, 0, 0, "Error", full_err, CACHE['wallet'].get('USDT', 0)]))
        return jsonify({"status": "error", "msg": str(e), "code": 500})

def brute_force_cancel(symbol=None):
    log = []
    found_orders = False
    
    # 1. Try Specific Symbol First
    if symbol:
        try:
            orders = EXCHANGE_INSTANCE.fetch_open_orders(symbol)
            for o in orders:
                try:
                    EXCHANGE_INSTANCE.cancel_order(o['id'], o['symbol'])
                    log.append(f"Cancelled: {o['id']} ({o['symbol']})")
                    found_orders = True
                except Exception as e: log.append(f"Err {o['id']}: {e}")
        except: pass

    # 2. If nothing found/cancelled, GO NUCLEAR (Fetch Global Orders)
    if not found_orders:
        try:
            # fetch_open_orders without arguments fetches EVERYTHING
            all_orders = EXCHANGE_INSTANCE.fetch_open_orders()
            if not all_orders:
                log.append("Binance says: No open orders anywhere.")
            
            for o in all_orders:
                # Filter if we only want to nuke the target symbol
                if symbol and symbol not in o['symbol'].replace("/", ""):
                     continue
                
                try:
                    EXCHANGE_INSTANCE.cancel_order(o['id'], o['symbol'])
                    log.append(f"Global Nuke: {o['id']} ({o['symbol']})")
                except Exception as e: log.append(f"Err {o['id']}: {e}")
        except Exception as e:
            log.append(f"Global Fetch Fail: {e}")

    time.sleep(1.0) # Wait for Binance to unlock funds
    return log

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