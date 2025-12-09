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
        # Check if any open orders exist first to save API calls
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
    
    # Current State
    current_cost = BOT_MEMORY['h4_cost']
    current_cap = BOT_MEMORY['d2_cap']
    
    new_d2 = current_cap
    new_h4 = current_cost

    if side == "BUY":
        # LOGIC: Buying increases our Cost Basis (Money locked in trade)
        # We ADD to H4 in case we do multiple buys (DCA)
        new_h4 = current_cost + usdt_value
        # D2 (Capital) does NOT change on buy. It waits for the Sell to realize PnL.
    
    elif side == "SELL":
        # LOGIC: Selling realizes Profit/Loss
        # 1. Calculate how much of the "bag" we sold (Ratio)
        base = symbol.replace("USDT", "")
        rem_bal = get_balance(base)
        total_held = rem_bal + coin_qty # Total we had before this sell
        
        if total_held > 0:
            ratio_sold = coin_qty / total_held
            cost_of_sold_portion = current_cost * ratio_sold
            
            # 2. Profit = Revenue - Cost
            profit = usdt_value - cost_of_sold_portion
            
            # 3. Update Capital (Compounding)
            new_d2 = current_cap + profit
            
            # 4. Reduce Cost Basis
            new_h4 = current_cost - cost_of_sold_portion
            
            # Cleanup: If we sold everything (dust check), reset H4 to 0
            if rem_bal < (coin_qty * 0.02): 
                new_h4 = 0

    # UPDATE MEMORY (Master)
    BOT_MEMORY['d2_cap'] = new_d2
    BOT_MEMORY['h4_cost'] = new_h4
    
    # UPDATE SHEET (Backup/Display)
    try:
        # Update D2 (Capital), H3 (Mirror of Cap for you), H4 (Cost)
        sheet.update('D2', [[new_d2]])
        sheet.update('H3', [[new_d2]]) 
        sheet.update('H4', [[new_h4]])
    except: pass
    
    return new_d2

def init_memory_state():
    """Loads H4 (Cost Basis) only ONCE at startup to recover state"""
    global BOT_MEMORY
    try:
        sheet = get_sheet()
        # Read H4 specifically
        h4_val = sheet.acell('H4').value
        BOT_MEMORY['h4_cost'] = safe_float(h4_val)
        print(f"State Recovered: Cost Basis = {BOT_MEMORY['h4_cost']}")
    except Exception as e:
        print(f"Init State Failed: {e}")

# --- BACKGROUND TASKS ---
def background_sync_loop():
    global BOT_MEMORY
    
    # Allow server boot
    time.sleep(1)
    
    # NEW: Recover state once on boot
    init_memory_state()
    
    tick = 0 
    while True:
        try:
            # --- TASK A: Sync Settings (D2, E2, F2 ONLY) ---
            # We DO NOT sync H4 here. H4 is managed strictly by the Webhook.
            data = sheet.batch_get(['D2', 'E2', 'F2'])
            
            val_d2 = data[0][0][0] if (len(data) > 0 and data[0]) else 0
            val_e2 = data[1][0][0] if (len(data) > 1 and data[1]) else 100
            val_f2 = data[2][0][0] if (len(data) > 2 and data[2]) else "MARKET"

            BOT_MEMORY['d2_cap'] = safe_float(val_d2)
            BOT_MEMORY['e2_pct'] = safe_float(val_e2)
            BOT_MEMORY['f2_type'] = str(val_f2).upper()
            # Note: H4 is intentionally skipped to prevent race conditions
            
            # Clear error flag if successful
            BOT_MEMORY['last_error'] = None

        except Exception as e:
            # Save error to memory so you can see it via CLI
            BOT_MEMORY['last_error'] = f"Sync Failed: {str(e)}"
            print(f"Sync Failed: {e}")

        # --- TASK B: VISUAL DASHBOARD UPDATE (Every 60s) ---
        # We put this in its own try/except so it NEVER stops Task A
        if tick % 4 == 0: 
            try:
                usdt = get_balance("USDT")
                # Safe BTC price fetch
                try: btc = get_coin_price("BTCUSDT")
                except: btc = 0
                
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                sheet.update('A2:C2', [[ts, usdt, btc]])
                
                # Monitor H1 Coin
                h1_val = sheet.acell('H1').value
                if h1_val:
                    mon_sym = h1_val.replace("USDT","").strip().upper()
                    c_bal = get_balance(mon_sym)
                    sheet.update('H2', [[c_bal]])
            except Exception as e:
                print(f"Dashboard Update Failed: {e}")

        # Standard Wait
        tick += 1
        time.sleep(15)

t = threading.Thread(target=background_sync_loop, daemon=True)
t.start()

# --- ROUTES ---

@app.route('/')
def home():
    """Fix for UptimeRobot 404s"""
    return "Bot is awake.", 200

def write_log_row(sheet, row_data):
    try:
        col_a = sheet.col_values(1)
        next_row = len(col_a) + 1
        if next_row < 6: next_row = 6
        sheet.update(f'A{next_row}:J{next_row}', [row_data])
    except Exception as e:
        print(f"Write Failed: {e}")
        sheet.append_row(row_data)

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
        
        # --- NEW: ONE ORDER RULE ---
        # Cancel any pending orders before processing the new signal
        was_cancelled = cancel_all_open_orders(symbol)
        if was_cancelled:
            # Log the cancellation? Optional, but good for tracking.
            # We proceed with the new signal logic below.
            pass
        
        # --- SAFETY FALLBACK: If Memory is Empty/Zero, Force Read Sheet ---
        if d2_cap == 0:
            try:
                print("Memory Empty (0.0). Forcing Sheet Read...")
                sheet = get_sheet()
                s_data = sheet.batch_get(['D2', 'E2', 'H4', 'F2'])
                d2_cap = safe_float(s_data[0][0][0] if (len(s_data)>0 and s_data[0]) else 0)
                e2_pct = safe_float(s_data[1][0][0] if (len(s_data)>1 and s_data[1]) else 100)
                h4_cost = safe_float(s_data[2][0][0] if (len(s_data)>2 and s_data[2]) else 0)
                f2_type = str(s_data[3][0][0]).upper() if (len(s_data)>3 and s_data[3]) else "MARKET"
                
                # Update Memory for next time
                BOT_MEMORY['d2_cap'] = d2_cap
                BOT_MEMORY['e2_pct'] = e2_pct
                BOT_MEMORY['h4_cost'] = h4_cost
                BOT_MEMORY['f2_type'] = f2_type
            except Exception as e:
                print(f"Fallback Read Failed: {e}")
        # ------------------------------------------------------------------
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
                raw_price = float(data.get('limit_price', sent_price))
                tick_size = get_price_tick_size(symbol)
                
                # CRITICAL: If raw_price has MORE decimals than tick_size, round it.
                # If it has LESS or EQUAL, keep it exact.
                final_limit_price = round_step_size(raw_price, tick_size)
                
                qty_coins = amt / final_limit_price 
                step = get_symbol_step_size(symbol)
                qty_coins = round_step_size(qty_coins, step)
                
                params['timeInForce'] = data.get('timeInForce', 'GTC')
                params['quantity'] = qty_coins
                params['price'] = "{:.8f}".format(final_limit_price).rstrip('0').rstrip('.')
                amt = qty_coins * final_limit_price
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
                # If Market order pending (rare), use 0 or sent_price
                # If Limit order, use the limit price
                exec_price = data.get('limit_price', sent_price if sent_price != 'Market' else 0)
            
            exec_qty = resp.get('origQty', 0)

        if status.startswith("Filled"):
            final_cap = update_compounding_after_trade(sheet, side, usdt_value, float(exec_qty), symbol)

        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        applied_pct = f"{req_pct}%" if side == 'BUY' else "100%"
        
        # Columns: Time, Symbol, Side, Req%, SentPrice, ExecPrice, ExecQty, Status, Reason, New Cap
        row = [ts, symbol, side, applied_pct, sent_price, exec_price, exec_qty, status, reason, final_cap]
        
        # PRECISE ROW CALCULATION
        try:
            # Get all data in Column A (Timestamps)
            col_a_values = sheet.col_values(1)
            
            # Calculate next empty row
            next_row = len(col_a_values) + 1
            
            # CRITICAL: Never write above row 6 (Preserves Dashboard A1:C2 and Headers A5)
            if next_row < 6: 
                next_row = 6
            
            # Write to specific range (e.g., A6:J6)
            write_log_row(sheet, row)
        
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
        try: 
            # Added table_range='A5' to prevent misalignment
            if sheet:
                ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                # Pad with 0s to ensure 10 columns for alignment
                write_log_row(sheet, [ts, symbol, "ERROR", 0, 0, 0, 0, str(e), 0, 0])
        except: pass
        return jsonify({"error": str(e)}), 500

@app.route('/cli', methods=['POST'])
def cli():
    data = request.json
    if data.get('passphrase') != WEBHOOK_PASSPHRASE: return jsonify({"error": "Unauthorized"}), 401
    
    method = data.get('method')
    params = data.get('params', {})
    
    # NEW: Raw Memory Dump (No Sheet Refresh)
    if method == "debug_memory":
        return jsonify(BOT_MEMORY)
    
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