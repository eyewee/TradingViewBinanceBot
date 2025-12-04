import os
import math
import json
import datetime
import time
import threading
from flask import Flask, request, jsonify
from binance.spot import Spot
import gspread
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')
BASE_URL = 'https://testnet.binance.vision'

GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

# --- CLIENTS ---
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)

def get_sheet():
    creds_dict = json.loads(GOOGLE_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client_gs = gspread.authorize(creds)
    return client_gs.open("TradingBotLog").worksheet("Dashboard")

# --- HELPERS ---
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

# --- NEW: MONEY MANAGEMENT LOGIC ---
def calculate_trade_size(current_usdt_balance):
    try:
        sheet = get_sheet()
        # Read D2 (Dedicated Cap) and E2 (% Reinvest)
        # format: [['1000', '90']]
        settings = sheet.get('D2:E2')
        
        if not settings or len(settings) == 0 or len(settings[0]) < 2:
            print("Sheet settings empty, using default logic")
            return None # Fallback to Pine Script %
            
        dedicated_cap = float(settings[0][0])
        reinvest_pct = float(settings[0][1])
        
        # Logic: 
        # 1. We can't spend money we don't have. (Min of Cap vs Wallet)
        effective_cap = min(dedicated_cap, current_usdt_balance)
        
        # 2. Calculate Order Size
        usdt_to_spend = effective_cap * (reinvest_pct / 100.0)
        
        print(f"Sheet Settings -> Cap: {dedicated_cap}, Pct: {reinvest_pct}%. Calc Amount: {usdt_to_spend}")
        return usdt_to_spend

    except Exception as e:
        print(f"Money Management Error: {e}")
        return None # Fallback

# --- BACKGROUND TASKS ---
def update_dashboard():
    try:
        bal = get_usdt_balance()
        price = get_btc_price()
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        sheet = get_sheet()
        # Updates A2, B2, C2 (Time, Bal, Price)
        sheet.update('A2:C2', [[ts, bal, price]]) 
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
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Unauthorized"}), 401
    
    # Clean Symbol
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
            
            # --- APPLY MONEY MANAGEMENT ---
            # Try to get size from Sheet. If fail, use Pine Script %
            amt = calculate_trade_size(usdt_bal)
            
            if amt is None: 
                # Fallback to Pine Script logic
                pct = float(data.get('percentage', 100))
                amt = usdt_bal * (pct / 100.0)

            amt = round(amt, 2)
            
            if amt > 10:
                resp = client.new_order(symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=amt)
                status = "Filled"
            else:
                status = "Skipped: Low Bal/Cap"
                resp = {"status": "skipped", "msg": f"Calculated Amt {amt} < 10"}

        elif side == 'SELL':
            # Sell logic usually sells ALL coins or a %, doesn't typically use 'Dedicated Capital' 
            # as that concept applies to USDT risk.
            # Here we assume sell 100% of coin if triggered
            
            # 1. Get Coin Balance
            base = symbol.replace("USDT","")
            coin_bal = 0.0
            for a in client.account()['balances']:
                if a['asset'] == base: coin_bal = float(a['free'])
            
            step = get_symbol_step_size(symbol)
            qty = round_step_size(coin_bal, step)
            
            # Simple check
            if qty * float(sent_price) > 5: # Approx check
                resp = client.new_order(symbol=symbol, side="SELL", type="MARKET", quantity=qty)
                status = "Filled"
            else:
                status = "Skipped: Low Coin"
                resp = {"status": "skipped"}

        # Logging
        if 'fills' in resp: 
            exec_price = resp['fills'][0]['price']
            exec_qty = resp['fills'][0]['qty']

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Ensure row matches headers: Time, Symbol, Side, Req%, SentPrice, ExecPrice, ExecQty, Status
        row = [ts, symbol, side, "Sheet/Dyn", sent_price, exec_price, exec_qty, status]
        
        sheet = get_sheet()
        sheet.append_row(row)
        update_dashboard() # Force instant update
        
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
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: 
        return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    try:
        # --- CUSTOM COMMAND: Get Sheet Settings ---
        if method == "get_capital_status":
            # 1. Get Wallet Balance
            usdt_bal = get_usdt_balance()
            
            # 2. Get Sheet Settings
            sheet = get_sheet()
            # Read D2 (Dedicated Cap)
            val = sheet.acell('D2').value
            dedicated_cap = float(val) if val else 0.0
            
            # 3. Return Combined Data
            return jsonify({
                "wallet_balance": usdt_bal,
                "dedicated_cap": dedicated_cap,
                "effective_cap": min(usdt_bal, dedicated_cap) # We can't spend more than we physically have
            })

        # --- STANDARD BINANCE COMMANDS ---
        elif hasattr(client, method):
            return jsonify(getattr(client, method)(**params))
        else:
            return jsonify({"error": "Method not found"}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)