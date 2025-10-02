import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import traceback

# === CONFIG ===
SYMBOL = "SUI/USDT"
TIMEFRAME = "4h"
EMA_LENGTH = 50
RSI_LENGTH = 14

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"
CHECK_INTERVAL = 60 * 15  # 15 minutes

# EUR/USD exchange rate
EUR_USD_RATE = None

# RSI STRATEGY LEVELS
BUY_ZONE_EXTREME = 30      # All-in signal
BUY_ZONE_SCALE = 45        # Scaling zone
SELL_ZONE_SCALE = 55       # Start scaling out
SELL_ZONE_EXTREME = 70     # All-out signal
RSI_EXTREME_THRESHOLD = 80 # Emergency exit level

# Alert tracking to avoid spam
last_alert_rsi_level = None
last_signal_candle = None

# Create exchange client
exchange = ccxt.binance({"enableRateLimit": True})

def get_eur_usd_rate():
    """Fetch current EUR/USD exchange rate"""
    try:
        response = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=10)
        data = response.json()
        rate = data.get("rates", {}).get("USD")
        if rate:
            return float(rate)
        # Fallback
        response = requests.get("https://open.er-api.com/v6/latest/EUR", timeout=10)
        data = response.json()
        rate = data.get("rates", {}).get("USD")
        if rate:
            return float(rate)
    except Exception as e:
        print(f"Failed to fetch EUR/USD rate: {e}")
    return 1.10  # Fallback

def usd_to_eur(usd_amount):
    """Convert USD to EUR"""
    global EUR_USD_RATE
    if EUR_USD_RATE is None:
        EUR_USD_RATE = get_eur_usd_rate()
    return float(usd_amount) / float(EUR_USD_RATE)

def send_alert(message):
    """Send alert to Discord"""
    tag = "[SUI SIGNAL BOT]"
    full_msg = f"{tag} {message}"
    print(full_msg)
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": full_msg}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def fetch_data():
    """Fetch OHLCV and convert to EUR"""
    global EUR_USD_RATE
    try:
        candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch data: {e}")

    df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["time"] = df["time"].astype(int)

    # Convert to EUR
    if EUR_USD_RATE is None:
        EUR_USD_RATE = get_eur_usd_rate()

    df["open"] = df["open"].apply(usd_to_eur)
    df["high"] = df["high"].apply(usd_to_eur)
    df["low"] = df["low"].apply(usd_to_eur)
    df["close"] = df["close"].apply(usd_to_eur)

    return df

def get_current_candle_id(df):
    """Get unique candle ID to prevent duplicate alerts"""
    if len(df) == 0:
        return None
    return int(df.iloc[-1]["time"] / 1000)

def detect_parabolic_move(df):
    """Detect dangerous parabolic moves"""
    if len(df) < 10:
        return False

    recent_closes = df["close"].tail(10).reset_index(drop=True)
    if recent_closes.isnull().any():
        return False

    price_change = (recent_closes.iloc[-1] - recent_closes.iloc[0]) / recent_closes.iloc[0]
    consecutive_gains = 0
    for i in range(1, len(recent_closes)):
        if recent_closes.iloc[i] > recent_closes.iloc[i-1]:
            consecutive_gains += 1
        else:
            break

    is_parabolic = price_change > 0.40 or consecutive_gains >= 6
    if is_parabolic:
        send_alert(f"PARABOLIC MOVE DETECTED! Price up {price_change*100:.1f}% in 10 candles - HIGH RISK ZONE")
    return is_parabolic

def send_rsi_alert(rsi, price_eur):
    """Send RSI monitoring alerts"""
    global last_alert_rsi_level

    alert_levels = [80, 75, 70, 65, 60, 55, 45, 40, 35, 30, 25, 20]
    current_alert_level = None
    
    for level in alert_levels:
        if (level >= 70 and rsi >= level) or (level <= 45 and rsi <= level):
            current_alert_level = level
            break

    if current_alert_level and current_alert_level != last_alert_rsi_level:
        if current_alert_level >= 80:
            send_alert(f"EXTREME RSI: {rsi:.1f} @ EUR{price_eur:.4f} - EMERGENCY EXIT ZONE!")
        elif current_alert_level >= 70:
            send_alert(f"RSI EXTREME: {rsi:.1f} @ EUR{price_eur:.4f} - ALL-OUT SIGNAL (>=70)")
        elif current_alert_level >= 55:
            send_alert(f"RSI HIGH: {rsi:.1f} @ EUR{price_eur:.4f} - Start scaling out zone")
        elif current_alert_level <= 20:
            send_alert(f"RSI ULTRA-LOW: {rsi:.1f} @ EUR{price_eur:.4f} - EXTREME BUY ZONE!")
        elif current_alert_level <= 30:
            send_alert(f"RSI EXTREME: {rsi:.1f} @ EUR{price_eur:.4f} - ALL-IN SIGNAL (<=30)")
        elif current_alert_level <= 45:
            send_alert(f"RSI LOW: {rsi:.1f} @ EUR{price_eur:.4f} - Start scaling in zone")

        last_alert_rsi_level = current_alert_level

    # Reset alert level when in neutral zone
    if 46 <= rsi <= 54:
        last_alert_rsi_level = None

def analyze(df):
    """Analyze and send signals"""
    global last_signal_candle

    # Calculate indicators
    try:
        df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
        df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)
    except Exception as e:
        raise RuntimeError(f"Indicator calculation failed: {e}")

    if df["EMA"].isnull().all() or df["RSI"].isnull().all():
        print("Not enough data for indicators yet")
        return

    price_eur = float(df["close"].iloc[-1])
    ema_eur = float(df["EMA"].iloc[-1])
    rsi = float(df["RSI"].iloc[-1])
    current_candle = get_current_candle_id(df)

    # Send RSI level alerts
    send_rsi_alert(rsi, price_eur)

    # Check for parabolic moves
    is_parabolic = detect_parabolic_move(df)

    # Avoid duplicate signals on same candle
    if current_candle == last_signal_candle:
        return

    # BUY SIGNALS
    if rsi <= BUY_ZONE_EXTREME and not is_parabolic:
        if price_eur > ema_eur:
            msg = [
                f"BUY SIGNAL - ALL-IN ZONE",
                f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f} | EMA: EUR{ema_eur:.4f}",
                f"Signal: RSI <={BUY_ZONE_EXTREME} + Price above EMA",
                f"Strategy: Deploy 100% of available capital",
                f"Your balance: EUR425.17 available"
            ]
            send_alert("\n".join(msg))
            last_signal_candle = current_candle
    elif rsi <= 35 and not is_parabolic:
        msg = [
            f"BUY SIGNAL - Heavy Scale-In",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Deploy 25% of remaining capital"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi <= 40 and not is_parabolic:
        msg = [
            f"BUY SIGNAL - Medium Scale-In",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Deploy 15% of remaining capital"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi <= BUY_ZONE_SCALE and not is_parabolic:
        msg = [
            f"BUY SIGNAL - Light Scale-In",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Deploy 10% of remaining capital"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle

    # SELL SIGNALS
    if rsi >= RSI_EXTREME_THRESHOLD:
        msg = [
            f"SELL SIGNAL - EMERGENCY EXIT",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Signal: RSI >={RSI_EXTREME_THRESHOLD} - EXTREME OVERBOUGHT",
            f"Strategy: Exit 100% immediately"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi >= SELL_ZONE_EXTREME:
        msg = [
            f"SELL SIGNAL - ALL-OUT ZONE",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f} | EMA: EUR{ema_eur:.4f}",
            f"Signal: RSI >={SELL_ZONE_EXTREME}",
            f"Strategy: Exit 100% of position"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi >= 66:
        msg = [
            f"SELL SIGNAL - Heavy Scale-Out",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Sell 40% of position"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi >= 61:
        msg = [
            f"SELL SIGNAL - Medium Scale-Out",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Sell 25% of position"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle
    elif rsi >= SELL_ZONE_SCALE:
        msg = [
            f"SELL SIGNAL - Light Scale-Out",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f}",
            f"Strategy: Sell 15% of position"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle

# === MAIN LOOP ===
if __name__ == "__main__":
    startup_msg = [
        f"SUI RSI SIGNAL BOT STARTED",
        f"Mode: SIGNALS ONLY (No automatic trading)",
        f"Available Capital: EUR425.17",
        f"Strategy: ALL-IN at RSI <=30, ALL-OUT at RSI >=70",
        f"Scaling: Gradual entries 30-45, gradual exits 55-70",
        f"Safety: Parabolic move detection, Emergency exit at RSI >=80",
        f"Monitoring: Every {CHECK_INTERVAL/60:.0f} minutes",
        f"Prices shown in EUR (converted from USDT)"
    ]
    send_alert("\n".join(startup_msg))

    while True:
        try:
            df = fetch_data()
            analyze(df)
        except Exception as e:
            print("Error in main loop:", e)
            traceback.print_exc()
            try:
                send_alert(f"Error: {e}")
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)