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
        # Force Reconnect on error
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

# --- MONEY MANAGEMENT & COMPOUNDING ---
def get_capital_settings():
    sheet = get_sheet()
    # Reading D2 (Cap), E2 (Pct), H4 (Cost Basis)
    data = sheet.batch_get(['D2', 'E2', 'H4'])
    
    d2 = safe_float(data[0][0][0] if data[0] else 0)
    e2 = safe_float(data[1][0][0] if data[1] else 100)
    h4 = safe_float(data[2][0][0] if data[2] else 0) 
    
    return d2, e2, h4

def update_compounding(sheet, side, usdt_value, coin_qty, symbol, old_h4, old_d2):
    """Updates D2 (Capital) and H4 (Cost Basis) based on trade result."""
    try:
        new_d2 = old_d2
        new_h4 = old_h4

        if side == "BUY":
            # Add cost to H4
            new_h4 = old_h4 + usdt_value
        
        elif side == "SELL":
            # Calculate PnL based on ratio of bag sold
            base = symbol.replace("USDT", "")
            remaining_bal = get_balance(base)
            total_held_before = remaining_bal + coin_qty 
            
            if total_held_before > 0:
                ratio = coin_qty / total_held_before 
                cost_of_sold = old_h4 * ratio
                pnl = usdt_value - cost_of_sold
                
                new_d2 = old_d2 + pnl
                new_h4 = old_h4 - cost_of_sold
                
                # Reset if sold approx everything
                if remaining_bal < (coin_qty * 0.01): new_h4 = 0

        # Update Sheet
        sheet.update('D2', [[new_d2]]) # New Capital
        sheet.update('H3', [[new_d2]]) # Visual Helper
        sheet.update('H4', [[new_h4]]) # Internal State

        return new_d2
        
    except Exception as e:
        print(f"Compounding Error: {e}")
        return old_d2

# --- BACKGROUND TASKS (H1/H2 Monitor) ---
def update_dashboard_loop():
    while True:
        try:
            sheet = get_sheet()
            
            # 1. Basic Stats
            usdt = get_balance("USDT")
            btc = get_coin_price("BTCUSDT")
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            sheet.update('A2:C2', [[ts, usdt, btc]])
            
            # 2. H1/H2 Monitor
            h1_val = sheet.acell('H1').value
            if h1_val:
                monitor_symbol = h1_val.replace("USDT","").strip().upper()
                coin_bal = get_balance(monitor_symbol)
                sheet.update('H2', [[coin_bal]])
                
        except Exception as e:
            print(f"Loop Error: {e}")
        time.sleep(60)

t = threading.Thread(target=update_dashboard_loop, daemon=True)
t.start()

# --- WEBHOOK ---
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
        # 1. Get Settings from Sheet
        d2_cap, e2_pct, h4_cost = get_capital_settings()
        final_cap = d2_cap # Default to current
        
        # 2. Determine Trade Size
        if side == 'BUY':
            bal = get_balance("USDT")
            eff_cap = min(d2_cap, bal)
            
            # Check for "PercentAmount" override (CLI) or "percentage" (Pine)
            req_pct = float(data.get('PercentAmount', data.get('percentage', e2_pct)))
            
            # Use the requested % for this specific trade
            amt = eff_cap * (req_pct / 100.0)
            
            # -- LIMIT vs MARKET Logic --
            order_type = data.get('type', 'MARKET').upper()
            params = {"symbol": symbol, "side": "BUY", "type": order_type}
            
            if order_type == 'LIMIT':
                limit_price = float(data['limit_price'])
                qty_coins = amt / limit_price 
                step = get_symbol_step_size(symbol)
                qty_coins = round_step_size(qty_coins, step)
                
                params['timeInForce'] = data.get('timeInForce', 'GTC')
                params['quantity'] = qty_coins
                params['price'] = str(limit_price)
                amt = qty_coins * limit_price # Adjust actual USDT committed
            else:
                params['quoteOrderQty'] = round(amt, 2)

            if amt > 10:
                resp = client.new_order(**params)
                status = "Filled/Open"
            else:
                status = "Skipped: Low Amt"
                resp = {"status": "skipped", "msg": f"Amt {amt} < 10"}

        elif side == 'SELL':
            base = symbol.replace("USDT","")
            coin_bal = get_balance(base)
            
            # Verify Balance Logic
            if coin_bal == 0:
                status = "Skipped: No Coins"
                resp = {"status": "Skipped", "msg": "No coins to sell"}
            else:
                step = get_symbol_step_size(symbol)
                qty = round_step_size(coin_bal, step) 
                
                order_type = data.get('type', 'MARKET').upper()
                
                if order_type == 'LIMIT':
                    limit_price = float(data['limit_price'])
                    resp = client.new_order(symbol=symbol, side="SELL", type="LIMIT", 
                                          quantity=qty, price=str(limit_price), timeInForce='GTC')
                else:
                    resp = client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                    
                status = "Filled"

        # --- LOGGING & COMPOUNDING ---
        sheet = get_sheet()
        
        # Calculate USDT Value of trade for compounding
        if 'cummulativeQuoteQty' in resp:
            usdt_value = float(resp['cummulativeQuoteQty'])
        elif 'origQty' in resp and 'price' in resp:
            usdt_value = float(resp['origQty']) * float(resp['price'])

        # Get Execution details
        if 'fills' in resp and len(resp['fills']) > 0:
            exec_price = resp['fills'][0]['price']
            exec_qty = resp['fills'][0]['qty']
        else:
            exec_price = data.get('limit_price', 'Market/Pending')
            exec_qty = resp.get('origQty', 0)

        # Update D2/H4 (Compounding)
        if status.startswith("Filled"):
            final_cap = update_compounding(sheet, side, usdt_value, float(exec_qty), symbol, h4_cost, d2_cap)

        # Log Row
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        applied_pct = f"{req_pct}%" if side == 'BUY' else "100%"
        
        # Columns: Time, Symbol, Side, Req%, SentPrice, ExecPrice, ExecQty, Status, Reason, New Cap
        row = [ts, symbol, side, applied_pct, sent_price, exec_price, exec_qty, status, reason, final_cap]
        sheet.append_row(row)
        
        # --- Force Immediate H2 Update if Symbol matches H1 ---
        try:
            h1_val = sheet.acell('H1').value
            if h1_val:
                monitor_symbol = h1_val.replace("USDT","").strip().upper()
                # Check if the traded symbol is the one we are monitoring
                if monitor_symbol in symbol: 
                    # Fetch fresh balance immediately
                    new_coin_bal = get_balance(monitor_symbol)
                    sheet.update('H2', [[new_coin_bal]])
        except: pass
        # -----------------------------------------------------------

        
        return jsonify(resp)

    except Exception as e:
        try: get_sheet().append_row([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "ERROR", 0, 0, 0, 0, str(e)])
        except: pass
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    # Pass-through for simple CLI status checks
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    if method == "get_capital_status":
        d2, e2, h4 = get_capital_settings()
        bal = get_balance("USDT")
        return jsonify({"dedicated_cap": d2, "reinvest_pct": e2, "wallet_balance": bal, "effective_cap": min(d2, bal)})
    
    if hasattr(client, method):
        return jsonify(getattr(client, method)(**params))
    
    return jsonify({"error": "Method not found"}), 400

if __name__ == "__main__":
    app.run(debug=True)