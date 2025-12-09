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
    "h4_cost": 0.0,
    "f2_type": "MARKET"
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
            print(f"Cancelled {len(open_orders)} open orders for {symbol}")
            return True
    except Exception as e:
        print(f"Cancel Failed: {e}")
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

def update_compounding_after_trade(sheet, side, usdt_value, coin_qty, symbol):
    global BOT_MEMORY
    
    old_h4 = BOT_MEMORY['h4_cost']
    old_d2 = BOT_MEMORY['d2_cap']
    
    new_d2 = old_d2
    new_h4 = old_h4

    if side == "BUY":
        new_h4 = old_h4 + usdt_value
    
    elif side == "SELL":
        base = symbol.replace("USDT", "")
        rem_bal = get_balance(base)
        total_held = rem_bal + coin_qty 
        
        if total_held > 0:
            ratio = coin_qty / total_held 
            cost_sold = old_h4 * ratio
            pnl = usdt_value - cost_sold
            new_d2 = old_d2 + pnl
            new_h4 = old_h4 - cost_sold
            if rem_bal < (coin_qty * 0.02): new_h4 = 0

    BOT_MEMORY['d2_cap'] = new_d2
    BOT_MEMORY['h4_cost'] = new_h4
    
    try:
        sheet.update('D2', [[new_d2]])
        sheet.update('H3', [[new_d2]]) 
        sheet.update('H4', [[new_h4]])
    except: pass
    
    return new_d2

def init_memory_state():
    global BOT_MEMORY
    try:
        sheet = get_sheet()
        h4_val = sheet.acell('H4').value
        BOT_MEMORY['h4_cost'] = safe_float(h4_val)
        print(f"State Recovered: Cost Basis = {BOT_MEMORY['h4_cost']}")
    except Exception as e:
        print(f"Init State Failed: {e}")

def write_log_row(sheet, row_data):
    """Writes to the exact next row based on Column A count to prevent gaps"""
    try:
        col_a = sheet.col_values(1)
        next_row = len(col_a) + 1
        # Protect Headers (Rows 1-5)
        if next_row < 6: next_row = 6
        
        sheet.update(f'A{next_row}:J{next_row}', [row_data])
    except Exception as e:
        print(f"Write Failed: {e}")
        sheet.append_row(row_data)

# --- BACKGROUND TASKS ---
def background_sync_loop():
    global BOT_MEMORY
    
    # Wait for server boot
    time.sleep(1)
    init_memory_state()
    
    tick = 0 
    while True:
        try:
            sheet = get_sheet()
            
            # --- TASK A: Sync Settings ---
            data = sheet.batch_get(['D2', 'E2', 'F2'])
            
            val_d2 = data[0][0][0] if (len(data) > 0 and data[0]) else 0
            val_e2 = data[1][0][0] if (len(data) > 1 and data[1]) else 100
            val_f2 = data[2][0][0] if (len(data) > 2 and data[2]) else "MARKET"

            BOT_MEMORY['d2_cap'] = safe_float(val_d2)
            BOT_MEMORY['e2_pct'] = safe_float(val_e2)
            BOT_MEMORY['f2_type'] = str(val_f2).upper()
            
            BOT_MEMORY['last_error'] = None

            # --- TASK B: Update Dashboard (Every 60s) ---
            if tick % 4 == 0: 
                try:
                    usdt = get_balance("USDT")
                    btc = 0
                    try: btc = get_coin_price("BTCUSDT")
                    except: pass
                    
                    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    sheet.update('A2:C2', [[ts, usdt, btc]])
                    
                    h1_val = sheet.acell('H1').value
                    if h1_val:
                        mon_sym = h1_val.replace("USDT","").strip().upper()
                        c_bal = get_balance(mon_sym)
                        sheet.update('H2', [[c_bal]])
                except Exception as dash_err:
                    print(f"Dash Err: {dash_err}")

        except Exception as e:
            BOT_MEMORY['last_error'] = f"Sync Loop: {str(e)}"
            time.sleep(60)
        
        tick += 1
        time.sleep(15)

t = threading.Thread(target=background_sync_loop, daemon=True)
t.start()

# --- ROUTES ---

@app.route('/')
def home():
    return "Bot is awake.", 200

@app.route('/webhook', methods=['POST'])
def webhook():
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
    
    # Init sheet for error logging fallback
    try: sheet = get_sheet()
    except: sheet = None

    try:
        # 1. READ FROM MEMORY
        d2_cap = BOT_MEMORY['d2_cap']
        e2_pct = BOT_MEMORY['e2_pct']
        f2_type = BOT_MEMORY['f2_type']
        h4_cost = BOT_MEMORY['h4_cost']
        
        # ONE ORDER RULE: Cancel pending before new signal
        cancel_all_open_orders(symbol)
        
        # SAFETY FALLBACK: If Memory is Empty/Zero, Force Read Sheet
        if d2_cap == 0:
            try:
                print("Memory 0. Forcing Sheet Read...")
                if sheet:
                    s_data = sheet.batch_get(['D2', 'E2', 'H4', 'F2'])
                    d2_cap = safe_float(s_data[0][0][0] if (len(s_data)>0 and s_data[0]) else 0)
                    e2_pct = safe_float(s_data[1][0][0] if (len(s_data)>1 and s_data[1]) else 100)
                    h4_cost = safe_float(s_data[2][0][0] if (len(s_data)>2 and s_data[2]) else 0)
                    f2_type = str(s_data[3][0][0]).upper() if (len(s_data)>3 and s_data[3]) else "MARKET"
                    
                    BOT_MEMORY['d2_cap'] = d2_cap
                    BOT_MEMORY['e2_pct'] = e2_pct
                    BOT_MEMORY['h4_cost'] = h4_cost
                    BOT_MEMORY['f2_type'] = f2_type
            except Exception as e:
                print(f"Fallback Read Failed: {e}")
        
        final_cap = d2_cap
        
        # 2. Determine Order Type
        payload_type = data.get('type', 'MARKET').upper()
        target_type = payload_type
        
        is_manual_cli = "CLI" in reason
        
        if not is_manual_cli and payload_type == 'MARKET' and 'LIMIT' in f2_type:
            if sent_price != 'Market' and safe_float(sent_price) > 0:
                target_type = 'LIMIT'

        # 3. Determine Trade Size & Execute
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
                # Format to remove scientific notation
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

        # --- LOGGING & COMPOUNDING ---
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
        
        # USE THE PRECISION WRITER
        write_log_row(sheet, row)
        
        # Force H2 Update
        try:
            h1_val = sheet.acell('H1').value
            if h1_val:
                mon_sym = h1_val.replace("USDT","").strip().upper()
                if mon_sym in symbol: 
                    new_coin_bal = get_balance(mon_sym)
                    sheet.update('H2', [[new_coin_bal]])
        except: pass
        
        return jsonify(resp)

    except Exception as e:
        # LOG ERROR WITH CORRECT ALIGNMENT
        if sheet:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            write_log_row(sheet, [ts, symbol, "ERROR", 0, 0, 0, 0, str(e), 0, 0])
            
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    if method == "debug_memory":
        return jsonify(BOT_MEMORY)
    
    if method == "get_capital_status":
        try:
            sheet = get_sheet()
            data = sheet.batch_get(['D2', 'E2', 'H4', 'F2'])
            
            d2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 0)
            e2 = safe_float(data[1][0][0] if (len(data)>1 and data[1]) else 100)
            
            BOT_MEMORY['d2_cap'] = d2
            BOT_MEMORY['e2_pct'] = e2
            
            bal = get_balance("USDT")
            return jsonify({
                "dedicated_cap": d2, 
                "reinvest_pct": e2, 
                "wallet_balance": bal, 
                "effective_cap": min(d2, bal)
            })
        except Exception as e:
             return jsonify({"error": f"Sheet Sync Failed: {str(e)}"}), 500
    
    if hasattr(client, method):
        return jsonify(getattr(client, method)(**params))
    
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)