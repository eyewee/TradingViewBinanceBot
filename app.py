import os
import math
from flask import Flask, request, jsonify
from binance.spot import Spot
from binance.error import ClientError

app = Flask(__name__)

# --- SECURE CONFIGURATION ---
API_KEY = os.environ.get('BINANCE_API_KEY')
API_SECRET = os.environ.get('BINANCE_API_SECRET')
WEBHOOK_PASSPHRASE = os.environ.get('WEBHOOK_PASSPHRASE')

# Connect to TESTNET explicitly
BASE_URL = 'https://testnet.binance.vision'

# --- FIXED LINE BELOW (api_key and api_secret) ---
client = Spot(api_key=API_KEY, api_secret=API_SECRET, base_url=BASE_URL)

# Helper: Round down to avoid "Filter Failure" (Precision errors)
def round_step_size(quantity, step_size):
    quantity = float(quantity)
    step_size = float(step_size)
    precision = int(round(-math.log(step_size, 10), 0))
    return float(round(quantity - (quantity % step_size), precision))

# Helper: Get precision info for a specific pair
def get_symbol_info(symbol):
    info = client.exchange_info(symbol=symbol)
    filters = info['symbols'][0]['filters']
    step_size = '0.000001' 
    for f in filters:
        if f['filterType'] == 'LOT_SIZE':
            step_size = f['stepSize']
            break
    return step_size

@app.route('/')
def welcome():
    return "Testnet Bot is Running securely!"

@app.route('/webhook', methods=['POST'])
def webhook():
    data = request.json
    
    # 1. Security Check
    incoming_passphrase = data.get('passphrase')
    if incoming_passphrase != WEBHOOK_PASSPHRASE:
        print("Error: Invalid Passphrase")
        return jsonify({"error": "Unauthorized"}), 401
    
    try:
        symbol = data['symbol'].upper() 
        side = data['side'].upper()     
        percentage = float(data['percentage']) 

        # Define pair logic (assuming trading against USDT or BUSD)
        if "USDT" in symbol:
            quote_asset = "USDT"
            base_asset = symbol.replace("USDT", "")
        elif "BUSD" in symbol:
            quote_asset = "BUSD"
            base_asset = symbol.replace("BUSD", "")
        else:
            return jsonify({"error": "Script only supports USDT/BUSD pairs"}), 400

        # --- BUY LOGIC (Spend USDT) ---
        if side == 'BUY':
            balance_info = client.account()
            free_balance = 0.0
            for asset in balance_info['balances']:
                if asset['asset'] == quote_asset:
                    free_balance = float(asset['free'])
                    break
            
            # Calculate 100% (or x%) of USDT
            amount_to_spend = free_balance * (percentage / 100.0)
            
            # Round to 2 decimals for USDT and ensure minimum order size (>10 USDT recommended)
            amount_to_spend = round(amount_to_spend, 2)
            
            if amount_to_spend < 10: 
                return jsonify({"error": "Balance too low for Testnet trade (Min ~10 USDT)"}), 400

            print(f"Buying: Spending {amount_to_spend} {quote_asset}")

            order = client.new_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=amount_to_spend 
            )

        # --- SELL LOGIC (Sell Coin) ---
        elif side == 'SELL':
            balance_info = client.account()
            free_balance = 0.0
            for asset in balance_info['balances']:
                if asset['asset'] == base_asset:
                    free_balance = float(asset['free'])
                    break
            
            # Calculate 100% (or x%) of Coin
            raw_qty = free_balance * (percentage / 100.0)
            
            # Fix Precision
            step_size = get_symbol_info(symbol)
            final_qty = round_step_size(raw_qty, step_size)
            
            print(f"Selling: {final_qty} {base_asset}")

            order = client.new_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=final_qty
            )

        print(f"Order Success: {order.get('orderId')}")
        return jsonify(order)

    except ClientError as e:
        print(f"Binance API Error: {e}")
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        print(f"Internal Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(debug=True)