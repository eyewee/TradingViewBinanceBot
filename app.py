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
from oauth2client.service_account import ServiceAccountCredentials

app = Flask(__name__)

# --- CONFIGURATION ---
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')
BASE_URL = 'https://testnet.binance.vision' 

# --- GOOGLE SHEETS SETUP ---
GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def get_sheet():
    # We re-auth every time to prevent timeouts in long-running threads
    creds_dict = json.loads(GOOGLE_JSON)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
    client_gs = gspread.authorize(creds)
    return client_gs.open("TradingBotLog").worksheet("Dashboard")

# --- BINANCE CLIENT ---
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)

# --- HELPER FUNCTIONS ---
def get_usdt_balance():
    try:
        acct = client.account()
        for asset in acct['balances']:
            if asset['asset'] == 'USDT':
                return float(asset['free'])
    except:
        pass
    return 0.0

def get_btc_price():
    try:
        ticker = client.ticker_price(symbol="BTCUSDT")
        return float(ticker['price'])
    except:
        return 0.0

def update_dashboard():
    """Fetches data and updates Sheet Row 2"""
    try:
        bal = get_usdt_balance()
        price = get_btc_price()
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        sheet = get_sheet()
        # Update A2, B2, C2 directly
        sheet.update('A2', [[timestamp]])
        sheet.update('B2', [[bal]])
        sheet.update('C2', [[price]])
        print(f"[{timestamp}] Dashboard Updated: Bal={bal} BTC={price}")
    except Exception as e:
        print(f"Dashboard Update Error: {e}")

def append_trade_log(row_data):
    try:
        sheet = get_sheet()
        sheet.append_row(row_data)
    except Exception as e:
        print(f"Sheet Append Error: {e}")

# --- BACKGROUND THREAD (Live Updates) ---
def start_background_loop():
    while True:
        # 1. Update the sheet
        update_dashboard()
        
        # 2. Sleep for 60 seconds
        time.sleep(60)

# Start the thread immediately when script loads
# daemon=True means it will shut down if the main app crashes
t = threading.Thread(target=start_background_loop, daemon=True)
t.start()


# --- ROUTES ---

@app.route('/')
def welcome():
    return "Bot is active with Background Updates."

# --- DYNAMIC CLI ENDPOINT ---
@app.route('/cli', methods=['POST'])
def cli():
    data = request.json
    
    # 1. Security Check
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Unauthorized"}), 401
    
    # 2. Dynamic Method Execution
    method_name = data.get('method')     # e.g., 'account', 'ticker_price'
    params = data.get('params', {})      # e.g., {'symbol': 'BTCUSDT'}
    
    try:
        # Check if the method exists on the Binance Client object
        if hasattr(client, method_name):
            func = getattr(client, method_name)
            
            # Call the function with unpacked arguments
            result = func(**params) 
            return jsonify(result)
        else:
            return jsonify({"error": f"Method '{method_name}' not found in Binance SDK"}), 400
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# --- TRADINGVIEW WEBHOOK ---
@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Unauthorized"}), 401
    
    raw_symbol = data['symbol'].upper().replace("/", "")
    symbol = raw_symbol + "T" if raw_symbol.endswith("USD") and not raw_symbol.endswith("USDT") else raw_symbol
    
    side = data['side'].upper()
    percentage = float(data['percentage'])
    status = "Pending"
    exec_price = "Market"
    exec_qty = 0
    
    try:
        if side == 'BUY':
            bal = get_usdt_balance()
            amt = round(bal * (percentage / 100.0), 2)
            if amt > 10:
                resp = client.new_order(symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=amt)
                status = "Filled"
                # Try to parse fills for accurate logging
                if 'fills' in resp: exec_price = resp['fills'][0]['price']
            else:
                status = "Skipped: Low Bal"
                resp = {"status": "skipped"}

        elif side == 'SELL':
            # Need to get coin balance... simplified for brevity
            status = "Filled (Sim)"
            resp = {"status": "executed"}

        # Log to sheet
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        append_trade_log([timestamp, symbol, side, percentage, exec_price, exec_qty, status])
        
        # Force an immediate dashboard update too
        update_dashboard()
        
        return jsonify(resp)

    except Exception as e:
        append_trade_log([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), symbol, "ERROR", 0, 0, 0, str(e)])
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)