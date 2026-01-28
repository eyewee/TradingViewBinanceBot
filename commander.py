import requests
import json
import sys
import os
from tabulate import tabulate

def load_config():
    config = {}
    try:
        with open(os.path.join(os.path.dirname(__file__), 'config.txt'), 'r') as f:
            for line in f:
                if ':' in line:
                    key, val = line.strip().split(':', 1)
                    config[key.strip()] = val.strip()
        return config
    except: sys.exit(1)

CFG = load_config()
BASE_URL = CFG.get('WEBHOOK_URL', '').replace('/cli', '').replace('/webhook', '')
PASSPHRASE = CFG.get('WEBHOOK_PASSPHRASE', '')

def send_request(endpoint, payload):
    payload['passphrase'] = PASSPHRASE
    try:
        res = requests.post(f"{BASE_URL}{endpoint}", json=payload)
        res.raise_for_status()
        return res.json()
    except Exception as e:
        print(f"Error: {e}")
        return None

def format_execution(data):
    if not data: print("No data."); return
    if "status" in data and str(data["status"]).lower() == "skipped":
        print(f"\n[!] SKIPPED: {data.get('msg')}"); return
    if "msg" in data and "code" in data:
        print(f"\n[!] ERROR: {data['msg']} ({data['code']})"); return

    headers = ["Symbol", "Side", "Type", "Exec Qty", "Price", "Cost", "Status"]
    row = [
        data.get('symbol'), data.get('side'), data.get('type'),
        f"{float(data.get('executedQty', data.get('filled', 0))):.6f}",
        f"{float(data.get('price', data.get('average', 0))):.4f}",
        f"{float(data.get('cummulativeQuoteQty', data.get('cost', 0))):.2f}",
        data.get('status')
    ]
    print("\n--- EXECUTION ---")
    print(tabulate([row], headers=headers, tablefmt="fancy_grid"))

def universal_format(data):
    if not data: print("No data."); return
    if isinstance(data, dict): data = [data]
    if not isinstance(data, list): print(data); return
    
    headers = sorted(list(set().union(*(d.keys() for d in data if isinstance(d, dict)))))
    headers = [h for h in headers if h not in ['info', 'fees', 'datetime']]
    
    rows = []
    for d in data:
        rows.append([d.get(h, "") for h in headers])
    print(tabulate(rows, headers=headers, tablefmt="simple"))

def smart_trade(side, symbol, amt):
    cap = send_request("/cli", {"method": "get_capital_status"})
    eff = float(cap.get('effective_cap', 0) if cap else 0)
    
    payload = {"symbol": symbol.upper(), "side": side, "type": "MARKET", "reason": f"CLI {side}"}
    
    if side == "BUY":
        if "%" in amt: payload["PercentAmount"] = float(amt.replace("%", ""))
        elif "$" in amt: payload["PercentAmount"] = (float(amt.replace("$", "")) / eff) * 100 if eff > 0 else 0
        elif amt.lower() == "all": payload["PercentAmount"] = 100.0
        else: print("Use % or $ for buys"); return
    elif side == "SELL":
        if "%" in amt: payload["PercentAmount"] = float(amt.replace("%", ""))
        elif amt.lower() == "all": payload["PercentAmount"] = 100.0
        else: payload["quantity"] = float(amt)

    format_execution(send_request("/webhook", payload))

if __name__ == "__main__":
    args = sys.argv
    if len(args) < 2: sys.exit()
    cmd = args[1].lower()

    if cmd == "status": print(send_request("/cli", {"method": "get_capital_status"}))
    elif cmd == "exchange":
        lst = send_request("/cli", {"method": "list_exchanges"})
        if not lst: print("No exchanges."); sys.exit()
        for i, ex in enumerate(lst): print(f"{i+1}. {ex}")
        try:
            sel = lst[int(input("Select: "))-1]
            print(send_request("/cli", {"method": "set_exchange", "name": sel}).get('msg'))
        except: print("Invalid")
    
    elif cmd == "buy": smart_trade("BUY", args[2], args[3] if len(args)>3 else "100%")
    elif cmd == "sell": smart_trade("SELL", args[2], args[3] if len(args)>3 else "100%")
    
    elif cmd == "new_order" and len(args) >= 8:
        p = {
            "symbol": args[2].upper(), "side": args[3].upper(), "type": args[4].upper(),
            "PercentAmount": args[5], "limit_price": args[6], "timeInForce": args[7], "reason": "CLI Limit"
        }
        format_execution(send_request("/webhook", p))

    elif cmd == "balance":
        t = args[2].upper().replace("USDT","")
        res = send_request("/cli", {"method": "account"})
        found = False
        for b in res.get('balances', []):
            if b['asset'] == t:
                print(f"Free: {b['free']} | Locked: {b['locked']}")
                found = True
        if not found: print("0.0")

    elif cmd == "panic":
        res = send_request("/panic", {"symbol": "FORCE_SHEET"})
        if res:
            for l in res.get('log', []): print(f"> {l}")

    elif cmd == "price":
        print(send_request("/cli", {"method": "ticker_price", "params": {"symbol": args[2]}}))

    else:
        p = {"symbol": args[2]} if len(args)>2 else {}
        universal_format(send_request("/cli", {"method": cmd, "params": p}))