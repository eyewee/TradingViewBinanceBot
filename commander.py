import requests
import json
import sys
import os
from tabulate import tabulate

# --- CONFIG LOADER ---
def load_config():
    config = {}
    config_path = os.path.join(os.path.dirname(__file__), 'config.txt')
    try:
        with open(config_path, 'r') as f:
            for line in f:
                if ':' in line:
                    key, val = line.strip().split(':', 1)
                    config[key.strip()] = val.strip()
        return config
    except FileNotFoundError:
        print("Error: config.txt not found.")
        sys.exit(1)

# Load immediately
CFG = load_config()
BASE_URL = CFG.get('WEBHOOK_URL', '')
PASSPHRASE = CFG.get('WEBHOOK_PASSPHRASE', '')

# Remove /cli if user pasted it by mistake, we add it dynamically
if BASE_URL.endswith('/cli'): BASE_URL = BASE_URL[:-4]
if BASE_URL.endswith('/webhook'): BASE_URL = BASE_URL[:-8]

def send_request(endpoint, payload):
    """Generic sender to either /cli or /webhook"""
    payload['passphrase'] = PASSPHRASE
    try:
        url = f"{BASE_URL}{endpoint}"
        res = requests.post(url, json=payload)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"Connection Error: {e}")
        if 'res' in locals(): print(res.text)
        return None

# --- FORMATTERS ---

def format_execution(data):
    """Specialized formatter for Buy/Sell responses"""
    if not data: print("No data."); return

    # Handle "Skipped" logic or Errors
    if "status" in data and data["status"].lower() == "skipped":
        print(f"\n[!] ORDER SKIPPED: {data.get('msg', 'Unknown Reason')}")
        return
    if "msg" in data and "code" in data:
        print(f"\n[!] BINANCE ERROR: {data['msg']} (Code: {data['code']})")
        return

    # Calculate Avg Price
    avg_price = 0.0
    if 'fills' in data and data['fills']:
        total_qty = sum(float(f['qty']) for f in data['fills'])
        total_quote = sum(float(f['price']) * float(f['qty']) for f in data['fills'])
        if total_qty > 0: avg_price = total_quote / total_qty
    
    if avg_price == 0 and 'price' in data:
        avg_price = float(data['price'])

    headers = ["Symbol", "Side", "Type", "Exec Qty", "Avg Price", "Total USDT", "Status"]
    
    exec_qty = float(data.get('executedQty', data.get('origQty', 0)))
    total_usdt = float(data.get('cummulativeQuoteQty', 0))
    price_str = f"{avg_price:.4f}" if isinstance(avg_price, float) else "Market"

    row = [
        data.get('symbol'),
        data.get('side'),
        data.get('type'),
        f"{exec_qty:.5f}",
        price_str,
        f"{total_usdt:.2f}",
        data.get('status')
    ]
    print("\n--- ORDER EXECUTION ---")
    print(tabulate([row], headers=headers, tablefmt="fancy_grid"))

def universal_format(data):
    if not data: print("No data returned."); return
    if isinstance(data, dict): data = [data] 
    if not isinstance(data, list): print(data); return
    
    # Get headers
    headers = sorted(list(set().union(*(d.keys() for d in data if isinstance(d, dict)))))
    
    # Optional: Hide useless columns to keep terminal clean
    hidden = ['clientOrderId', 'orderListId', 'icebergQty', 'isWorking', 'updateTime', 'commission', 'commissionAsset', 'isBestMatch', 'isBuyer', 'isMaker', 'orderId']
    headers = [h for h in headers if h not in hidden]

    rows = []
    for d in data:
        row = []
        for h in headers:
            val = d.get(h, "")
            # Shorten long decimals
            if isinstance(val, str) and val.replace('.','',1).isdigit():
                try: val = f"{float(val):.5f}"
                except: pass
            row.append(val)
        rows.append(row)
    
    print(tabulate(rows, headers=headers, tablefmt="simple"))

# --- COMMAND LOGIC ---

def check_status():
    print("Fetching Capital Status...")
    # Hits /cli because we just want data, not to trade
    data = send_request("/cli", {"method": "get_capital_status"})
    if data:
        print("\n--- GOOGLE SHEET SETTINGS ---")
        print(f"Wallet Balance:   {data.get('wallet_balance')} USDT")
        print(f"Dedicated Cap:    {data.get('dedicated_cap')} USDT (Cell D2)")
        print(f"Reinvest %:       {data.get('reinvest_pct')}% (Cell E2)")
        print(f"Effective Base:   {data.get('effective_cap')} USDT")
        print("-----------------------------")
        return data
    return None

def smart_trade(side, symbol, amount_str):
    cap_data = check_status()
    if not cap_data: return

    effective_cap = float(cap_data['effective_cap'])
    
    payload = {
        "symbol": symbol, 
        "side": side, 
        "type": "MARKET",
        "reason": f"Manual CLI {side}"
    }

    # --- BUY LOGIC ---
    if side == "BUY":
        req_pct = 0.0
        
        if "%" in amount_str:
            req_pct = float(amount_str.replace("%", ""))
            print(f"Buying: {req_pct}% of Cap")
            payload["PercentAmount"] = req_pct
            
        elif "$" in amount_str:
            # Buy specific Dollar Amount (e.g. 10$)
            target_usd = float(amount_str.replace("$", ""))
            if effective_cap > 0:
                req_pct = (target_usd / effective_cap) * 100.0
                print(f"Buying: ${target_usd} ({req_pct:.2f}% of Cap)")
            payload["PercentAmount"] = req_pct
            
        elif amount_str.lower() == "all":
            payload["PercentAmount"] = 100.0
            
        else:
            # Buy specific Coin Quantity (e.g. 10 Coins)
            # We must convert coins -> USDT because Binance Market Buy uses quoteOrderQty usually
            target_coins = float(amount_str)
            
            # Fetch Price
            price_res = send_request("/cli", {"method": "ticker_price", "params": {"symbol": symbol}})
            price = float(price_res['price'])
            
            # Calculate USDT cost
            cost_usdt = target_coins * price
            
            if effective_cap > 0:
                req_pct = (cost_usdt / effective_cap) * 100.0
                print(f"Buying: {target_coins} coins (~${cost_usdt:.2f})")
            
            payload["PercentAmount"] = req_pct

    # --- SELL LOGIC (Based on COIN QTY) ---
    elif side == "SELL":
        if "%" in amount_str:
            # Percentage of Holdings
            pct = float(amount_str.replace("%", ""))
            payload["PercentAmount"] = pct
            print(f"Selling: {pct}% of Holdings")
            
        elif "$" in amount_str:
            # Sell specific Dollar Value (e.g. 10$)
            target_usd = float(amount_str.replace("$", ""))
            
            # Fetch Price to convert USD -> Qty
            price_res = send_request("/cli", {"method": "ticker_price", "params": {"symbol": symbol}})
            price = float(price_res['price'])
            qty = target_usd / price
            
            payload["quantity"] = qty
            print(f"Selling: ${target_usd} value (~{qty:.6f} coins @ {price})")
            
        elif amount_str.lower() == "all":
            payload["PercentAmount"] = 100.0
            print("Selling: 100% of Holdings")
            
        else:
            # Assume raw number is Coin Quantity
            qty = float(amount_str)
            payload["quantity"] = qty
            print(f"Selling: {qty} coins")

    # Execute
    res = send_request("/webhook", payload)
    format_execution(res)

# --- MAIN ENTRY POINT ---
if __name__ == "__main__":
    args = sys.argv
    if len(args) < 2:
        print("Usage:")
        print("  python commander.py status")
        print("  python commander.py buy SYMBOL PCT (Market)")
        print("  python commander.py sell SYMBOL PCT (Market)")
        print("  python commander.py new_order SYMBOL SIDE LIMIT PCT PRICE GTC")
        print("  python commander.py balance SYMBOL")
        sys.exit()

    cmd = args[1].lower()

    # 1. STATUS
    if cmd == "status":
        check_status()

    # 2. MARKET BUY (Smart)
    elif cmd == "buy":
        symbol = args[2].upper()
        amt = args[3] if len(args) > 3 else "100%"
        smart_trade("BUY", symbol, amt)

    # 3. MARKET SELL (Smart)
    elif cmd == "sell":
        symbol = args[2].upper()
        amt = args[3] if len(args) > 3 else "100%"
        smart_trade("SELL", symbol, amt)

    # 4. LIMIT ORDER (Short Syntax)
    # python commander.py new_order TRUMPUSDT BUY LIMIT 10 5.00 GTC
    elif cmd == "new_order" and len(args) >= 8:
        symbol = args[2].upper()
        side = args[3].upper()
        type_ = args[4].upper()
        pct = args[5]
        price = args[6]
        tif = args[7]

        payload = {
            "symbol": symbol, "side": side, "type": type_,
            "PercentAmount": pct, "limit_price": price,
            "timeInForce": tif, "reason": "Manual CLI Limit"
        }
        
        print(f"Sending Limit Order: {side} {symbol} @ {price} ({pct}% of Cap)...")
        res = send_request("/webhook", payload)
        format_execution(res)

    # 5. BALANCE CHECK
    elif cmd == "balance":
        if len(args) < 3: print("Usage: balance SYMBOL"); sys.exit()
        target = args[2].upper().replace("USDT","")
        res = send_request("/cli", {"method": "account"})
        found = False
        for a in res['balances']:
            if a['asset'] == target:
                print(f"\n--- BALANCE: {target} ---")
                print(f"Free:   {a['free']}")
                print(f"Locked: {a['locked']}")
                found = True
        if not found: print(f"Asset {target} not found (0.0)")

    # 6. ACCOUNT / PRICE / FALLBACK
    elif cmd == "account":
        res = send_request("/cli", {"method": "account"})
        universal_format(res['balances'])
    
    elif cmd == "price":
        sym = args[2].upper()
        res = send_request("/cli", {"method": "ticker_price", "params": {"symbol": sym}})
        print(f"Price: {res['price']}")
        
    elif cmd == "memory":
        print("Fetching Raw Server Memory...")
        res = send_request("/cli", {"method": "debug_memory"})
        print(json.dumps(res, indent=2))

    else:
        # MAP SHORTCUTS TO REAL BINANCE METHODS
        method_map = {
            "orders": "get_orders",              # python commander.py orders BTCUSDT
            "open_orders": "get_open_orders",    # python commander.py open_orders BTCUSDT
            "my_trades": "my_trades",            # python commander.py my_trades BTCUSDT
            "cancel_open_orders": "cancel_open_orders",
            "price": "ticker_price",
            "time": "time",
            "account": "account"
        }
        
        # Use mapped name if it exists, otherwise use exactly what you typed
        api_method = method_map.get(cmd, cmd)
        
        # Build Parameters
        params = {}
        
        # Assume 2nd argument is always Symbol (if provided)
        if len(args) > 2:
            params['symbol'] = args[2].upper()
            
        # Parse extra arguments like "limit=5" or "orderId=123"
        if len(args) > 3:
            for arg in args[3:]:
                if "=" in arg:
                    k, v = arg.split("=")
                    params[k] = v
        
        # Send to server and print table
        res = send_request("/cli", {"method": api_method, "params": params})
        
        if cmd == "price":
            print(f"Price: {res.get('price')}")
        else:
            universal_format(res)