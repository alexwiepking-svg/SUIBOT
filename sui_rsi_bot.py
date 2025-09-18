import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import math

# === CONFIG ===
SYMBOL = "SUI/USDT"   # only SUI
TIMEFRAME = "4h"
EMA_LENGTH = 50
RSI_LENGTH = 14

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/XXXXXXXXXXXX"  # replace with your webhook

CHECK_INTERVAL = 60 * 15  # seconds between checks

# Capital (simulation if AUTO_EXECUTE = False)
START_FUNDS = 409.64
POSITION_PCT = 1.0
AUTO_EXECUTE = False

# Trade rules
BUY_ZONE = 40
DEEP_RSI = 30
TP1 = 0.06
TP2 = 0.15
SL_PCT = 0.05
EMA_SLOPE_LOOKBACK = 3

# === STATE ===
balance_usdt = START_FUNDS
in_position = False
entry_price = None
position_qty = 0.0

# === HELPERS ===
def send_alert(message):
    tag = "[SUI/USDT]"
    full_msg = f"{tag} {message}"
    print(full_msg)
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": full_msg}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def fetch_data():
    exchange = ccxt.binance()
    candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=200)
    df = pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
    df["close"] = df["close"].astype(float)
    return df

def analyze(df):
    global in_position, entry_price, position_qty, balance_usdt

    df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
    df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)

    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]
    rsi = df["RSI"].iloc[-1]
    prev_rsi = df["RSI"].iloc[-2]

    ema_then = df["EMA"].iloc[-1 - EMA_SLOPE_LOOKBACK]
    ema_slope = (ema - ema_then) / ema_then if ema_then and not math.isnan(ema_then) else 0.0

    # --- BUY LOGIC ---
    if not in_position:
        if prev_rsi < DEEP_RSI and rsi >= DEEP_RSI:
            execute_buy(price, rsi, "Deep oversold bounce (<30 → up)")
        elif prev_rsi < BUY_ZONE and rsi >= BUY_ZONE and ema_slope >= 0:
            execute_buy(price, rsi, f"RSI bounce {prev_rsi:.2f}→{rsi:.2f} with EMA slope {ema_slope:.4f}")
        elif prev_rsi < 50 and rsi >= 50 and price > ema and ema_slope > 0.001:
            execute_buy(price, rsi, "Momentum continuation (RSI >50 & above EMA)")

    # --- SELL LOGIC ---
    if in_position:
        entry = entry_price
        tp1_price = entry * (1 + TP1)
        tp2_price = entry * (1 + TP2)
        sl_price = entry * (1 - SL_PCT)

        if price <= sl_price:
            execute_sell(price, rsi, f"Stop-loss hit ({price:.4f} ≤ {sl_price:.4f})")
        elif price >= tp2_price:
            execute_sell(price, rsi, f"Take-profit 2 hit (+15%) @ {price:.4f}")
        elif prev_rsi > 70 and rsi <= 70:
            execute_sell(price, rsi, f"RSI downcross from overbought ({prev_rsi:.2f}→{rsi:.2f})")

def execute_buy(price, rsi, reason):
    global in_position, entry_price, position_qty, balance_usdt

    usdt_alloc = balance_usdt * POSITION_PCT if AUTO_EXECUTE else START_FUNDS * POSITION_PCT
    qty = usdt_alloc / price

    if AUTO_EXECUTE:
        balance_usdt -= usdt_alloc

    entry_price = price
    position_qty = qty
    in_position = True

    tp1_price = price * (1 + TP1)
    tp2_price = price * (1 + TP2)
    sl_price = price * (1 - SL_PCT)

    msg = [
        f"🟢 BUY — {reason}",
        f"Price: {price:.4f} | RSI: {rsi:.2f}",
        f"Allocated: {usdt_alloc:.2f} USDT → ~{qty:.4f} SUI",
        f"Targets: TP1 {tp1_price:.4f} (+6%), TP2 {tp2_price:.4f} (+15%)",
        f"Stop-loss: {sl_price:.4f} (−5%)",
    ]
    if AUTO_EXECUTE:
        msg.append(f"📊 Balance after buy: {balance_usdt:.2f} USDT (holding {qty:.4f})")
    else:
        msg.append("(Manual mode: balance not updated)")
    send_alert("\n".join(msg))

def execute_sell(price, rsi, reason):
    global in_position, entry_price, position_qty, balance_usdt

    qty = position_qty
    proceeds = qty * price

    if AUTO_EXECUTE:
        balance_usdt += proceeds

    in_position = False
    entry_price = None
    position_qty = 0.0

    msg = [
        f"🔴 SELL — {reason}",
        f"Price: {price:.4f} | RSI: {rsi:.2f}",
        f"Closed position: ~{qty:.4f} SUI → {proceeds:.2f} USDT",
    ]
    if AUTO_EXECUTE:
        msg.append(f"📊 Balance now: {balance_usdt:.2f} USDT")
    else:
        msg.append("(Manual mode: balance not updated)")
    send_alert("\n".join(msg))

# === MAIN LOOP ===
if __name__ == "__main__":
    send_alert(f"🚀 Bot STARTED — Monitoring {SYMBOL} | AUTO_EXECUTE={AUTO_EXECUTE}")
    while True:
        try:
            df = fetch_data()
            analyze(df)
        except Exception as e:
            send_alert(f"❌ Error: {e}")
            print(f"Error: {e}")
        time.sleep(CHECK_INTERVAL)
