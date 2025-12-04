import os
import math
import json
import datetime
import time
import threading
from flask import Flask, request, jsonify
from binance.spot import Spot
from binance.error import ClientError # Re-added for safety
import gspread

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')
BASE_URL = 'https://testnet.binance.vision'

GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
# Explicit scopes ensure we can Read AND Write
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

# --- GLOBAL CLIENTS ---
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)
GOOGLE_CLIENT = None

# --- HELPER: Safe Float Conversion ---
def safe_float(value, default=0.0):
    try:
        # Removes '$' and ',' so "$1,000" becomes 1000.0
        if isinstance(value, str):
            clean_val = value.replace('$', '').replace(',', '').strip()
            if clean_val == "": return default
            return float(clean_val)
        return float(value)
    except:
        return default

def get_sheet():
    global GOOGLE_CLIENT
    try:
        if GOOGLE_CLIENT is None:
            print("Initializing Google Connection (Modern)...")
            creds_dict = json.loads(GOOGLE_JSON)
            # Pass scopes explicitly to avoid permission errors
            GOOGLE_CLIENT = gspread.service_account_from_dict(creds_dict, scopes=SCOPES)
        
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")
        
    except Exception as e:
        print(f"Google Connection Error ({e}). Reconnecting...")
        creds_dict = json.loads(GOOGLE_JSON)
        GOOGLE_CLIENT = gspread.service_account_from_dict(creds_dict, scopes=SCOPES)
        return GOOGLE_CLIENT.open("TradingBotLog").worksheet("Dashboard")

# --- BINANCE HELPERS ---
def get_usdt_balance():
    try:
        acct = client.account()
        for asset in acct['balances']:
            if asset['asset'] == 'USDT': return float(asset['free'])
    except: pass
    return 0.0

def get_btc_price():
    try:
        return float(client.ticker_price(symbol="BTCUSDT")['price'])
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

# --- MONEY MANAGEMENT LOGIC ---
def calculate_trade_size(current_usdt_balance):
    try:
        sheet = get_sheet()
        settings = sheet.get('D2:E2') 
        
        if not settings or len(settings) == 0:
            return None
            
        row_vals = settings[0]
        # Use safe_float to handle empty cells or currency formatting
        dedicated_cap = safe_float(row_vals[0] if len(row_vals) > 0 else 0)
        reinvest_pct = safe_float(row_vals[1] if len(row_vals) > 1 else 100)
        
        effective_cap = min(dedicated_cap, current_usdt_balance)
        usdt_to_spend = effective_cap * (reinvest_pct / 100.0)
        
        return usdt_to_spend

    except Exception as e:
        print(f"Money Mgmt Error: {e}")
        return None

# --- BACKGROUND TASKS ---
def update_dashboard():
    try:
        bal = get_usdt_balance()
        price = get_btc_price()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        get_sheet().update('A2:C2', [[ts, bal, price]]) 
    except Exception as e:
        print(f"Dash Error: {e}")

def start_background_loop():
    while True:
        update_dashboard()
        time.sleep(60)

t = threading.Thread(target=start_background_loop, daemon=True)
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
    
    status = "Pending"
    exec_price = 0
    exec_qty = 0
    resp = {}

    try:
        if side == 'BUY':
            usdt_bal = get_usdt_balance()
            amt = calculate_trade_size(usdt_bal)
            
            # Fallback
            if amt is None: 
                pct = float(data.get('percentage', 100))
                amt = usdt_bal * (pct / 100.0)

            amt = round(amt, 2)
            
            if amt > 10:
                resp = client.new_order(symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=amt)
                status = "Filled"
            else:
                status = "Skipped: Low Bal"
                resp = {"status": "skipped", "msg": f"Amt {amt} < 10"}

        elif side == 'SELL':
            base = symbol.replace("USDT","")
            coin_bal = 0.0
            for a in client.account()['balances']:
                if a['asset'] == base: coin_bal = float(a['free'])
            
            step = get_symbol_step_size(symbol)
            qty = round_step_size(coin_bal, step)
            
            if qty * float(sent_price if sent_price != 'Market' else 1) > 5:
                resp = client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                status = "Filled"
            else:
                status = "Skipped: Low Coin"
                resp = {"status": "skipped"}

        if 'fills' in resp: 
            exec_price = resp['fills'][0]['price']
            exec_qty = resp['fills'][0]['qty']

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        row = [ts, symbol, side, "Sheet/Dyn", sent_price, exec_price, exec_qty, status]
        get_sheet().append_row(row)
        
        return jsonify(resp)

    except Exception as e:
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            get_sheet().append_row([ts, symbol, "ERROR", 0, sent_price, 0, 0, str(e)])
        except: pass
        return jsonify({"error": str(e)}), 500

# --- CLI ENDPOINT ---
@app.route('/cli', methods=['POST'])
def cli():
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    try:
        if method == "get_capital_status":
            usdt_bal = get_usdt_balance()
            
            # Fetch from Sheet
            sheet = get_sheet()
            # We get both D2 and E2 in one go
            vals = sheet.get('D2:E2') 
            
            # Default values
            dedicated_cap = 0.0
            reinvest_pct = 100.0
            
            # DEBUG LOG (Check Render Logs if this fails)
            print(f"Sheet Raw Values: {vals}")

            if vals and len(vals) > 0:
                row = vals[0]
                if len(row) > 0: dedicated_cap = safe_float(row[0])
                if len(row) > 1: reinvest_pct = safe_float(row[1])

            return jsonify({
                "wallet_balance": usdt_bal,
                "dedicated_cap": dedicated_cap,
                "reinvest_pct": reinvest_pct,
                "effective_cap": min(usdt_bal, dedicated_cap)
            })

        elif hasattr(client, method):
            return jsonify(getattr(client, method)(**params))
        else:
            return jsonify({"error": "Method not found"}), 400
    except Exception as e:
        print(f"CLI Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)