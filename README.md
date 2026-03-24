
# 📈 FREE Lightweight TradingView to Crypto Exchange Bot (with Google Sheets Logging)

This is a robust, fully automated bridge between TradingView alerts and cryptocurrency exchanges. Powered by Python, Flask, and the massive [CCXT Library](https://github.com/ccxt/ccxt), this bot supports **any crypto exchange (main and testnets) supported by ccxt library**.

Unlike basic webhook receivers, this bot features a **Google Sheets live integration** for dynamic setting controls and real-time trade logging. It also comes with a companion Command Line Interface (`commander.py`) for remote control, smart order execution, and emergency interventions: 
you can basically execute trades directly from the CLI through the API key, without ever connecting to the exchange itself. 

## ✨ Key Features

*   **Multi-Exchange Support (via CCXT)**: Trade on Binance, Bybit, KuCoin, etc., using the exact same code. Supports testnets/sandbox modes.
*   **TradingView Webhook Integration**: Instantly execute `BUY` and `SELL` signals from your custom Pine Script strategies.
*   **Secure Execution**: Protected by a custom `WEBHOOK_PASSPHRASE` to ensure no one can send malicious payloads to your endpoint.
*   **Google Sheets Dashboard**: 
    *   **Live Logging**: Logs every order, execution price, fill quantity, and tracks cumulative USDT balance.
    *   **Dynamic Settings**: Change reinvestment percentages, slippage, order types, and the active symbol directly from the Sheet *without restarting the bot*.
*   **Smart Order Management**:
    *   **Advanced Limit Timeout**: If a limit order doesn't fill within a specified time (e.g., 60s), the bot auto-cancels it and buys/sells the remainder at Market price.
    *   **Slippage Calculation**: Automatically adjusts Limit order prices based on your defined slippage tolerance.
*   **Remote CLI (`commander.py`)**: Execute trades from your terminal anywhere. Supports smart inputs (e.g., `buy BTC/USDT 50%` (50% -> 50% of your capital) or `buy BTC/USDT $100` etc.).
*   **Panic Mode 🚨**: Market tanking? Trigger a "Panic" via the CLI or hidden web UI to instantly force-cancel open limit orders and market-sell your holdings (page available at your-deployed-bot.onrender.com/special)

---

## 🛠 Prerequisites & Setup

### 1. Google Sheets Dashboard
You don't need to build the sheet from scratch! 
1. Open the [Template Google Sheet](https://docs.google.com/spreadsheets/d/1kf-4waFmD69z4AA314s9hiA2ohWK_yShqFvVOmA-EgY/edit?usp=sharing).
2. Go to **File > Make a copy** to save it to your own Google Drive.
3. In Google Cloud Console, enable the **Google Sheets API** and **Google Drive API**.
4. Create a Service Account, download the JSON credentials file.
5. **Share** your new Google Sheet with the email address found inside your Service Account JSON.

### 2. Exchange API Keys
Create an API key on your preferred exchange (e.g., Binance, Bybit) with **Spot Trading enabled**. Do *not* enable withdrawals.

---

## 🚀 Deployment (Render.com + UptimeRobot)

This bot is designed to run 24/7 on free hosting providers like Render.

### Phase 1: Deploying to Render
1. Create a free account on [Render.com](https://render.com).
2. Click **New +** and select **Web Service**.
3. Connect your GitHub account and select your fork of this repository (`eyewee/TradingViewBinanceBot`).
4. Set the following details:
   * **Environment**: `Python 3`
   * **Build Command**: `pip install -r requirements.txt`
   * **Start Command**: `gunicorn app:app`

### Phase 2: Environment Variables
In your Render dashboard, go to the **Environment** tab and add these 3 specific variables:

#### 1. `WEBHOOK_PASSPHRASE`
Your custom password passed inside the TradingView JSON payload. **This is critical** because without it, anyone who finds your Render.com URL could interact with your endpoint and send malicious signals.
```text
SuperSecretBotKey123
```

#### 2. `GOOGLE_CREDENTIALS`
Open the Google Service Account JSON file you downloaded earlier, copy the **entire contents**, and paste it here.

#### 3. `EXCHANGE_CONFIG`
A JSON configuration dictating which exchanges to load. Under the hood, the bot uses the[CCXT library](https://github.com/ccxt/ccxt) to manage connections and API commands. You can configure multiple accounts or testnets.

*Example:*
```json
{
  "binance_testnet": {
    "exchange_id": "binance",
    "apiKey": "YOUR_REAL_API_KEY",
    "secret": "YOUR_REAL_SECRET",
    "sandbox": true
  },
  "binance_real": {
    "exchange_id": "binance",
    "apiKey": "YOUR_REAL_API_KEY",
    "secret": "YOUR_REAL_SECRET",
    "sandbox": false
  },
  "bybit_spot": {
    "exchange_id": "bybit",
    "apiKey": "YOUR_BYBIT_KEY",
    "secret": "YOUR_BYBIT_SECRET",
    "options": { "defaultType": "spot" },
    "sandbox": false
  },
  "Kucoin": {
    "exchange_id": "kucoin",
    "apiKey": "YOUR_KUCOIN_KEY",
    "secret": "YOUR_KUCOIN_SECRET",
    "password": "YOUR_KUCOIN_PASSPHRASE", 
    "sandbox": false
  }
}
```

### Phase 3: Keep-Alive with UptimeRobot
Render's free tier spins down apps after 15 minutes of inactivity. To prevent this:
1. Go to[UptimeRobot](https://uptimerobot.com) and create a free account.
2. Add a new **HTTP(s)** monitor pointing to your Render app URL (e.g., `https://your-bot-name.onrender.com/`).
3. Set the ping interval to **5 minutes**. 

---

## 📡 TradingView Pine Script Setup

To trigger the bot from your TradingView strategies, you need to set up your Pine Script alerts to output a specific JSON payload. 

Here is an example of an alert function you can embed in your Pine Script:

```pine
f_fire_alert(string _id, float _price, string _side, int _mode) =>
    bool sent = false
    
    // Check Lock (Index 0): Have we already fired on this specific bar index?
    if array.get(g_alert_state, 0) != bar_index
        string modeTag = _mode == MODE_TICK ? "(Rt)" : _mode == MODE_ANTICIPATE ? "(Antic.)" : "(Close)"
        
        // Construct the JSON payload required by the bot
        string msg = '{"reason": "' + _id + ' ' + modeTag + '", "price": "' + str.tostring(_price, format.mintick) + '", "passphrase": "SuperSecretBotKey123", "symbol": "' + syminfo.ticker + '", "side": "' + _side + '", "percentage": 100}'
        
        // Fire the alert
        alert(msg, alert.freq_all)
        
        // Lock this bar index (Index 0) and Set Visual Flag (Index 1)
        array.set(g_alert_state, 0, bar_index)
        array.set(g_alert_state, 1, 1)
        sent := true
    
    sent
```

**When creating the alert in TradingView:**
1. Check **"Webhook URL"**.
2. Paste your Render endpoint: `https://your-bot-name.onrender.com/webhook`
3. The `alert()` function above will automatically populate the message box with the correct JSON. Make sure your `passphrase` exactly matches the `WEBHOOK_PASSPHRASE` on Render!

---

## 💻 CLI Companion (`commander.py`) Setup

You can control your bot directly from your local computer terminal using `commander.py`.

1. In the same folder as `commander.py` on your local machine, create a file named `config.txt`.
2. Add your Render URL and Passphrase exactly like this:
   ```text
   WEBHOOK_URL: https://your-bot-name.onrender.com
   WEBHOOK_PASSPHRASE: SuperSecretBotKey123
   ```
3. Install CLI dependencies: `pip install requests tabulate`

### CLI Usage Examples:
*   **Check Balance**: `python commander.py balance USDT`
*   **Check Bot Status**: `python commander.py status`
*   **Switch Exchanges**: `python commander.py exchange` *(Lets you switch between the accounts defined in your JSON config)*
*   **Smart Buy**: `python commander.py buy BTC/USDT 50%` *(Buys BTC with 50% of your available USDT)*
*   **Smart Buy (USD)**: `python commander.py buy SOL/USDT $150` *(Buys $150 worth of SOL)*
*   **Smart Sell**: `python commander.py sell BTC/USDT all` *(Sells 100% of your BTC)*
*   **Panic Sell**: `python commander.py panic` *(Cancels all orders and forcefully market-sells the active symbol)*

---

## ⚠️ Disclaimer
This software is provided for educational and informational purposes only. Do not risk money you cannot afford to lose. Cryptocurrency trading is highly volatile. The creator of this repository is not responsible for any financial losses incurred while using this bot. Always test with a sandbox/testnet account before using real funds.
