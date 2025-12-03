import os
import math
import json
import datetime
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
BASE_URL = 'https://testnet.binance.vision' # Change to None for Real Binance

# --- GOOGLE SHEETS SETUP ---
# We load the JSON key from the environment variable
GOOGLE_JSON = os.environ.get('GOOGLE_CREDENTIALS')
scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def log_to_sheet(data_row):
    try:
        creds_dict = json.loads(GOOGLE_JSON)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client_gs = gspread.authorize(creds)
        # Open the sheet by name (Make sure your Google Sheet is named EXACTLY this)
        sheet = client_gs.open("TradingBotLog").sheet1
        sheet.append_row(data_row)
    except Exception as e:
        print(f"Google Sheet Error: {e}")

# --- BINANCE SETUP ---
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)

def get_symbol_info(symbol):
    try:
        info = client.exchange_info(symbol=symbol)
        filters = info['symbols'][0]['filters']
        step_size = '0.000001'
        for f in filters:
            if f['filterType'] == 'LOT_SIZE':
                step_size = f['stepSize']
                break
        return step_size
    except:
        return '0.00001' # Fallback

def round_step_size(quantity, step_size):
    quantity = float(quantity)
    step_size = float(step_size)
    precision = int(round(-math.log(step_size, 10), 0))
    return float(round(quantity - (quantity % step_size), precision))

@app.route('/webhook', methods=['POST'])
def webhook():
    # Force=True allows us to parse the Pine Script string as JSON
    data = request.get_json(force=True)
    
    # 1. Security Check
    if data.get('passphrase') != WEBHOOK_PASSPHRASE:
        return jsonify({"error": "Unauthorized"}), 401
    
    # 2. Extract and CLEAN Symbol
    # This handles the "Coinbase BTCUSD" -> "Binance BTCUSDT" issue
    raw_symbol = data['symbol'].upper().replace("/", "") # Remove slashes
    if raw_symbol.endswith("USD") and not raw_symbol.endswith("USDT"):
        symbol = raw_symbol + "T" # Convert BTCUSD -> BTCUSDT
    else:
        symbol = raw_symbol

    side = data['side'].upper()
    percentage = float(data['percentage'])
    
    # Variables for logging
    exec_price = 0
    exec_qty = 0
    status = "Pending"
    usdt_balance = 0

    try:
        # Determine Assets
        if "USDT" in symbol:
            quote_asset = "USDT"
            base_asset = symbol.replace("USDT", "")
        elif "BUSD" in symbol:
            quote_asset = "BUSD"
            base_asset = symbol.replace("BUSD", "")
        else:
             raise Exception("Only USDT/BUSD pairs supported")

        # --- EXECUTION LOGIC ---
        order_response = None
        
        if side == 'BUY':
            # Get Balance
            balance_info = client.account()
            free_balance = 0.0
            for asset in balance_info['balances']:
                if asset['asset'] == quote_asset:
                    free_balance = float(asset['free'])
                if asset['asset'] == "USDT": # Track USDT specifically for logs
                    usdt_balance = float(asset['free'])
            
            amount_to_spend = free_balance * (percentage / 100.0)
            amount_to_spend = round(amount_to_spend, 2)
            
            if amount_to_spend > 10: # Minimum filter
                order_response = client.new_order(
                    symbol=symbol, side="BUY", type="MARKET", quoteOrderQty=amount_to_spend
                )
            else:
                status = "Skipped: Low Balance"

        elif side == 'SELL':
            balance_info = client.account()
            free_balance = 0.0
            for asset in balance_info['balances']:
                if asset['asset'] == base_asset:
                    free_balance = float(asset['free'])
                if asset['asset'] == "USDT":
                    usdt_balance = float(asset['free'])

            raw_qty = free_balance * (percentage / 100.0)
            step_size = get_symbol_info(symbol)
            final_qty = round_step_size(raw_qty, step_size)
            
            # Simple check to avoid zero errors
            if final_qty * 50000 > 5: # Rough estimation check
                order_response = client.new_order(
                    symbol=symbol, side="SELL", type="MARKET", quantity=final_qty
                )
            else:
                 status = "Skipped: Low Coin Balance"

        # --- PROCESS RESPONSE FOR LOGGING ---
        if order_response:
            status = "Filled"
            # Binance responses differ slightly. We try to grab the fills.
            if 'fills' in order_response and len(order_response['fills']) > 0:
                fill = order_response['fills'][0]
                exec_price = fill.get('price')
                exec_qty = fill.get('qty')
            else:
                # Fallback if no fills returned immediately
                exec_price = "Market"
                exec_qty = order_response.get('origQty')

        # Log to Google Sheet
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_to_sheet([timestamp, symbol, side, percentage, exec_price, exec_qty, usdt_balance, status])
        
        return jsonify(order_response if order_response else {"status": status})

    except Exception as e:
        # Log error to sheet
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log_to_sheet([timestamp, symbol, side, percentage, "ERROR", 0, 0, str(e)])
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)