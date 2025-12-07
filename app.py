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

def round_step_size(quantity, step_size):
    precision = int(round(-math.log(float(step_size), 10), 0))
    return float(round(quantity - (quantity % float(step_size)), precision))

def update_compounding_after_trade(sheet, side, usdt_value, coin_qty, symbol):
    """Updates Memory AND Sheet after a trade"""
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
            if rem_bal < (coin_qty * 0.01): new_h4 = 0

    # Update Memory Immediately
    BOT_MEMORY['d2_cap'] = new_d2
    BOT_MEMORY['h4_cost'] = new_h4
    
    # Update Sheet
    try:
        sheet.update('D2', [[new_d2]])
        sheet.update('H3', [[new_d2]]) 
        sheet.update('H4', [[new_h4]])
    except: pass
    
    return new_d2

# --- BACKGROUND TASKS ---
def background_sync_loop():
    global BOT_MEMORY
    
    # CRITICAL FIX: Wait 10s for Server to boot before hitting Google
    time.sleep(10)
    
    tick = 0 
    while True:
        try:
            sheet = get_sheet()
            
            # --- TASK A: Sync Settings ---
            data = sheet.batch_get(['D2', 'E2', 'H4', 'F2'])
            
            val_d2 = data[0][0][0] if (len(data) > 0 and data[0]) else 0
            val_e2 = data[1][0][0] if (len(data) > 1 and data[1]) else 100
            val_h4 = data[2][0][0] if (len(data) > 2 and data[2]) else 0
            val_f2 = data[3][0][0] if (len(data) > 3 and data[3]) else "MARKET"

            BOT_MEMORY['d2_cap'] = safe_float(val_d2)
            BOT_MEMORY['e2_pct'] = safe_float(val_e2)
            BOT_MEMORY['h4_cost'] = safe_float(val_h4)
            BOT_MEMORY['f2_type'] = str(val_f2).upper()

            # --- TASK B: Update Dashboard (Every 60s) ---
            if tick % 4 == 0: 
                usdt = get_balance("USDT")
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.update('A2:B2', [[ts, usdt]])
                
                h1_val = sheet.acell('H1').value
                if h1_val:
                    mon_sym = h1_val.replace("USDT","").strip().upper()
                    c_bal = get_balance(mon_sym)
                    sheet.update('H2', [[c_bal]])
            
            # Normal Sleep 15s (prevents Rate Limits)
            time.sleep(15)
            tick += 1

        except Exception as e:
            print(f"Sync Loop Error: {e}")
            # CRITICAL FIX: If Google fails, sleep 60s to prevent Crash Loop
            time.sleep(60) 

t = threading.Thread(target=background_sync_loop, daemon=True)
t.start()

# --- ROUTES ---

@app.route('/')
def home():
    """Fix for UptimeRobot 404s"""
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
    
    try:
        # 1. READ FROM MEMORY
        d2_cap = BOT_MEMORY['d2_cap']
        e2_pct = BOT_MEMORY['e2_pct']
        f2_type = BOT_MEMORY['f2_type']
        h4_cost = BOT_MEMORY['h4_cost']
        final_cap = d2_cap
        
        # 2. Determine Order Type
        payload_type = data.get('type', 'MARKET').upper()
        target_type = payload_type
        
        is_manual_cli = "CLI" in reason
        
        # Override Market with Limit if Sheet says so (and not CLI)
        if not is_manual_cli and payload_type == 'MARKET' and 'LIMIT' in f2_type:
            if sent_price != 'Market' and safe_float(sent_price) > 0:
                target_type = 'LIMIT'

        # 3. Determine Trade Size
        if side == 'BUY':
            bal = get_balance("USDT")
            eff_cap = min(d2_cap, bal)
            
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2_pct)))
            amt = eff_cap * (req_pct / 100.0)
            
            params = {"symbol": symbol, "side": "BUY", "type": target_type}
            
            if target_type == 'LIMIT':
                price_val = float(data.get('limit_price', sent_price))
                qty_coins = amt / price_val 
                step = get_symbol_step_size(symbol)
                qty_coins = round_step_size(qty_coins, step)
                
                params['timeInForce'] = data.get('timeInForce', 'GTC')
                params['quantity'] = qty_coins
                params['price'] = str(price_val)
                amt = qty_coins * price_val 
            else:
                params['quoteOrderQty'] = round(amt, 2)

            if amt > 10:
                resp = client.new_order(**params)
                status = "Filled/Open"
            else:
                # DETAILED LOGGING FIX
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
                    price_val = float(data.get('limit_price', sent_price))
                    params['quantity'] = qty
                    params['price'] = str(price_val)
                    params['timeInForce'] = data.get('timeInForce', 'GTC')
                else:
                    params['quantity'] = qty
                
                resp = client.new_order(**params)  
                status = "Filled"

        # --- LOGGING & COMPOUNDING ---
        sheet = get_sheet()
        
        if 'cummulativeQuoteQty' in resp:
            usdt_value = float(resp['cummulativeQuoteQty'])
        elif 'origQty' in resp and 'price' in resp:
            usdt_value = float(resp['origQty']) * float(resp['price'])

        if 'fills' in resp and len(resp['fills']) > 0:
            exec_price = resp['fills'][0]['price']
            exec_qty = resp['fills'][0]['qty']
        else:
            # FIX: If skipped, Price is 0. If Limit Pending, use Limit Price.
            if status.startswith("Skipped"):
                exec_price = 0
            else:
                exec_price = data.get('limit_price', sent_price)
            
            exec_qty = resp.get('origQty', 0)

        if status.startswith("Filled"):
            final_cap = update_compounding_after_trade(sheet, side, usdt_value, float(exec_qty), symbol)

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        applied_pct = f"{req_pct}%" if side == 'BUY' else "100%"
        
        row = [ts, symbol, side, applied_pct, sent_price, exec_price, exec_qty, status, reason, final_cap]
        # Force gspread to append relative to the table starting at A5
        # This prevents it from writing to column R
        sheet.append_row(row, table_range='A5')
        
        # Force H2 Update
        try:
            h1_val = sheet.acell('H1').value
            if h1_val:
                monitor_symbol = h1_val.replace("USDT","").strip().upper()
                if monitor_symbol in symbol: 
                    new_coin_bal = get_balance(monitor_symbol)
                    sheet.update('H2', [[new_coin_bal]])
        except: pass
        
        return jsonify(resp)

    except Exception as e:
        try: get_sheet().append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "ERROR", 0, 0, 0, 0, str(e)])
        except: pass
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    if method == "get_capital_status":
        # FORCE REFRESH: Read sheet directly to bypass stale memory
        try:
            sheet = get_sheet()
            data = sheet.batch_get(['D2', 'E2', 'H4', 'F2'])
            
            d2 = safe_float(data[0][0][0] if (len(data)>0 and data[0]) else 0)
            e2 = safe_float(data[1][0][0] if (len(data)>1 and data[1]) else 100)
            
            # Update Memory with fresh data
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