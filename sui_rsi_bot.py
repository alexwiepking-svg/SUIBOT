import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time

# === CONFIG ===
PAIR = "SUI/USDT"          # trading pair
TIMEFRAME = "4h"           # candle size (1h, 4h, 1d etc.)
EMA_LENGTH = 50
RSI_LENGTH = 14
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"
CHECK_INTERVAL = 60 * 15  # seconds between checks (1h)

# Track last signal to avoid duplicate alerts
last_signal = None

# === FUNCTIONS ===
def send_alert(message):
    """Send alert to Discord"""
    payload = {"content": message}
    try:
        requests.post(DISCORD_WEBHOOK, json=payload)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def fetch_data():
    """Fetch OHLCV candles from Binance"""
    exchange = ccxt.binance()
    candles = exchange.fetch_ohlcv(PAIR, timeframe=TIMEFRAME, limit=200)
    df = pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
    df["close"] = df["close"].astype(float)
    return df

def analyze(df):
    """Calculate RSI + EMA and send alerts"""
    global last_signal

    # Indicators
    df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
    df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)

    rsi = df["RSI"].iloc[-1]
    prev_rsi = df["RSI"].iloc[-2]
    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]

    # Print log
    print(f"[CHECK] Price={price:.4f} EMA={ema:.4f} RSI={rsi:.2f}")

    # Heads-up alerts
    if rsi < 30:
        send_alert(f"⚠ RSI oversold ({rsi:.2f}) on {PAIR} @ {price:.2f}")
    elif rsi > 70:
        send_alert(f"⚠ RSI overbought ({rsi:.2f}) on {PAIR} @ {price:.2f}")

    # Action alerts with EMA filter + one-per-side
    # BUY
    if prev_rsi < 30 and rsi >= 30 and price > ema:
        if last_signal != "BUY":
            send_alert(f"✅ BUY signal: RSI crossed up 30 ({rsi:.2f}), price {price:.2f}, above EMA {ema:.2f}")
            last_signal = "BUY"

    # SELL
    if prev_rsi > 70 and rsi <= 70 and price < ema:
        if last_signal != "SELL":
            send_alert(f"✅ SELL signal: RSI crossed down 70 ({rsi:.2f}), price {price:.2f}, below EMA {ema:.2f}")
            last_signal = "SELL"

# === MAIN LOOP ===
if __name__ == "__main__":
    # Test Discord webhook
    send_alert("🤖 SUI RSI Bot started! ✅ Discord webhook working")

    while True:
        try:
            df = fetch_data()
            analyze(df)
        except Exception as e:
            send_alert(f"⚠ Error: {e}")
            print(f"Error: {e}")
        time.sleep(CHECK_INTERVAL)
