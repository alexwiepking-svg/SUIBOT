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

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"  # replace with your webhook

CHECK_INTERVAL = 60 * 15  # seconds between checks

# Capital (simulation if AUTO_EXECUTE = False)
START_FUNDS = 409.64
POSITION_PCT = 1.0
AUTO_EXECUTE = False

# Trade rules
BUY_ZONE_DEEP = 30
BUY_ZONE_MEDIUM = 40
BUY_ZONE_LIGHT = 50
DEEP_RSI = 30
TP1 = 0.06
TP2 = 0.15
SL_PCT = 0.05
EMA_SLOPE_LOOKBACK = 3

SCALE_BUY = {
    "deep": 0.75,    # RSI ≤ 30 → 75% of available funds
    "medium": 0.25,  # RSI 31–40 → 25%
    "light": 0.10    # RSI 41–50 & above EMA → 10%
}

SCALE_SELL = {
    "TP1": 0.30,   # Sell 30% at TP1 (~6%)
    "TP2": 0.50,   # Sell 50% at TP2 (~15%)
    "RSI70": 0.20  # Sell remaining 20% if RSI ≥ 70
}

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

# === TRADE LOGIC ===
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

    # --- BUY LOGIC (scaled) ---
    if rsi <= BUY_ZONE_DEEP:
        execute_buy(price, rsi, SCALE_BUY["deep"], "Deep oversold bounce (<30)")
    elif BUY_ZONE_DEEP < rsi <= BUY_ZONE_MEDIUM:
        execute_buy(price, rsi, SCALE_BUY["medium"], f"Moderate oversold ({prev_rsi:.2f}->{rsi:.2f})")
    elif BUY_ZONE_MEDIUM < rsi <= BUY_ZONE_LIGHT and price > ema and ema_slope > 0:
        execute_buy(price, rsi, SCALE_BUY["light"], "Momentum continuation (RSI >40 & above EMA)")

    # --- SELL LOGIC (scaled) ---
    if in_position:
        tp1_price = entry_price * (1 + TP1)
        tp2_price = entry_price * (1 + TP2)
        sl_price = entry_price * (1 - SL_PCT)

        # TP1
        if price >= tp1_price and position_qty > 0:
            sell_portion(price, SCALE_SELL["TP1"], f"TP1 reached +{TP1*100:.0f}%")

        # TP2
        if price >= tp2_price and position_qty > 0:
            sell_portion(price, SCALE_SELL["TP2"], f"TP2 reached +{TP2*100:.0f}%")

        # RSI overbought exit
        if rsi >= 70 and position_qty > 0:
            sell_portion(price, SCALE_SELL["RSI70"], f"RSI ≥ 70 ({prev_rsi:.2f}->{rsi:.2f})")

        # Stop-loss on remaining
        if price <= sl_price and position_qty > 0:
            sell_portion(price, 1.0, f"Stop-loss hit ({price:.4f} ≤ {sl_price:.4f})")

def execute_buy(price, rsi, portion, reason):
    global in_position, entry_price, position_qty, balance_usdt

    if portion <= 0:
        return

    usdt_alloc = balance_usdt * portion if AUTO_EXECUTE else START_FUNDS * portion
    if usdt_alloc <= 0:
        return

    qty = usdt_alloc / price

    if AUTO_EXECUTE:
        balance_usdt -= usdt_alloc

    # Update position tracking
    if in_position:
        # Add to existing position
        entry_price = (entry_price * position_qty + price * qty) / (position_qty + qty)
        position_qty += qty
    else:
        entry_price = price
        position_qty = qty
        in_position = True

    tp1_price = entry_price * (1 + TP1)
    tp2_price = entry_price * (1 + TP2)
    sl_price = entry_price * (1 - SL_PCT)

    msg = [
        f"🟢 BUY — {reason}",
        f"Price: {price:.4f} | RSI: {rsi:.2f}",
        f"Allocated: {usdt_alloc:.2f} USDT → ~{qty:.4f} SUI",
        f"Targets: TP1 {tp1_price:.4f} (+6%), TP2 {tp2_price:.4f} (+15%)",
        f"Stop-loss: {sl_price:.4f} (−5%)",
        f"(New position: {position_qty:.4f} SUI @ avg {entry_price:.4f})"
    ]
    send_alert("\n".join(msg))

def sell_portion(price, fraction, reason):
    """Sell a fraction of current position."""
    global in_position, position_qty, balance_usdt, entry_price

    qty_to_sell = position_qty * fraction
    if qty_to_sell <= 0:
        return

    proceeds = qty_to_sell * price
    if AUTO_EXECUTE:
        balance_usdt += proceeds

    position_qty -= qty_to_sell
    if position_qty <= 0:
        in_position = False
        entry_price = None

    msg = [
        f"🔴 SELL — {reason}",
        f"Price: {price:.4f}",
        f"Sold {qty_to_sell:.4f} SUI → {proceeds:.2f} USDT",
    ]
    if AUTO_EXECUTE:
        msg.append(f"📊 Balance now: {balance_usdt:.2f} USDT, remaining {position_qty:.4f} SUI")
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
