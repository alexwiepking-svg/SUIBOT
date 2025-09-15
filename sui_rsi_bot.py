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
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
ADX_LENGTH = 14
ATR_LENGTH = 14
VOL_MA_LENGTH = 20

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"
CHECK_INTERVAL = 60 * 240  # 4 hours (match timeframe to check after candle close)

# EUR/USD exchange rate
EUR_USD_RATE = None

# Strategy thresholds (simplified)
BUY_RSI = 35       # Buy below this
SELL_RSI = 65      # Sell above this
EMERGENCY_RSI = 80 # Emergency sell
ADX_MAX = 25       # Only signal in low trend (range-bound)
VOL_MULTIPLIER = 1.5  # Volume > this * MA
ATR_MULTIPLIER = 3.0  # For parabolic filter

# Tracking
last_signal_candle = None
AVAILABLE_CAPITAL = 425.17  # EUR (update this manually if needed)

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

def fetch_data(limit=100):
    """Fetch OHLCV and convert to EUR"""
    global EUR_USD_RATE
    try:
        candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=limit)
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
    """Detect parabolic moves using ATR (volatility-adjusted)"""
    if len(df) < ATR_LENGTH + 10:
        return False

    recent_closes = df["close"].tail(10)
    price_change = (recent_closes.iloc[-1] - recent_closes.iloc[0]) / recent_closes.iloc[0]
    atr = df["ATR"].iloc[-1]
    avg_price = recent_closes.mean()

    is_parabolic = abs(price_change) > ATR_MULTIPLIER * (atr / avg_price)
    if is_parabolic:
        send_alert(f"PARABOLIC MOVE DETECTED! Change: {price_change*100:.1f}% vs ATR threshold - AVOID TRADES")
    return is_parabolic

def analyze(df):
    """Analyze and send signals"""
    global last_signal_candle

    # Calculate indicators (using pandas_ta for efficiency)
    try:
        df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
        df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)
        macd = ta.macd(df["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
        df["MACD_HIST"] = macd[f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"]
        adx = ta.adx(df["high"], df["low"], df["close"], length=ADX_LENGTH)
        df["ADX"] = adx[f"ADX_{ADX_LENGTH}"]
        df["ATR"] = ta.atr(df["high"], df["low"], df["close"], length=ATR_LENGTH)
        df["VOL_MA"] = df["volume"].rolling(VOL_MA_LENGTH).mean()
    except Exception as e:
        raise RuntimeError(f"Indicator calculation failed: {e}")

    if df["EMA"].isnull().any() or df["RSI"].isnull().any() or df["MACD_HIST"].isnull().any() or df["ADX"].isnull().any() or df["ATR"].isnull().any():
        print("Not enough data for indicators yet")
        return

    price_eur = float(df["close"].iloc[-1])
    ema_eur = float(df["EMA"].iloc[-1])
    rsi = float(df["RSI"].iloc[-1])
    macd_hist = float(df["MACD_HIST"].iloc[-1])
    adx = float(df["ADX"].iloc[-1])
    volume = float(df["volume"].iloc[-1])
    vol_ma = float(df["VOL_MA"].iloc[-1])
    current_candle = get_current_candle_id(df)

    # Avoid duplicate signals
    if current_candle == last_signal_candle:
        return

    # Check parabolic
    is_parabolic = detect_parabolic_move(df)

    # Emergency sell
    if rsi >= EMERGENCY_RSI:
        msg = f"EMERGENCY SELL! RSI: {rsi:.1f} @ EUR{price_eur:.4f} - Exit all positions immediately"
        send_alert(msg)
        last_signal_candle = current_candle
        return  # Skip other signals

    # Buy signal (aggressive all-in)
    if (rsi < BUY_RSI and price_eur > ema_eur and macd_hist > 0 and adx < ADX_MAX and
        volume > VOL_MULTIPLIER * vol_ma and not is_parabolic):
        msg = [
            f"BUY SIGNAL - ALL-IN",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f} | MACD Hist: {macd_hist:.2f} | ADX: {adx:.1f}",
            f"Strategy: Deploy 100% available capital (EUR{AVAILABLE_CAPITAL:.2f})"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle

    # Sell signal (all-out)
    elif (rsi > SELL_RSI and macd_hist < 0 and adx < ADX_MAX and
          volume > VOL_MULTIPLIER * vol_ma):
        msg = [
            f"SELL SIGNAL - ALL-OUT",
            f"Price: EUR{price_eur:.4f} | RSI: {rsi:.1f} | MACD Hist: {macd_hist:.2f} | ADX: {adx:.1f}",
            f"Strategy: Exit 100% of position"
        ]
        send_alert("\n".join(msg))
        last_signal_candle = current_candle

# === MAIN LOOP ===
if __name__ == "__main__":
    startup_msg = [
        f"SUI SIGNAL BOT v2 STARTED",
        f"Mode: SIGNALS ONLY (No auto-trading)",
        f"Available Capital: EUR{AVAILABLE_CAPITAL:.2f}",
        f"Strategy: All-in buys below RSI {BUY_RSI} with confirmations, all-out sells above {SELL_RSI}",
        f"Filters: MACD momentum, ADX range-bound, high volume, no parabolas",
        f"Emergency: Sell at RSI >= {EMERGENCY_RSI}",
        f"Monitoring: Every {CHECK_INTERVAL/3600:.0f} hours (after candle close)",
        f"Prices in EUR"
    ]
    send_alert("\n".join(startup_msg))

    while True:
        try:
            df = fetch_data(limit=200)  # More data for indicators
            analyze(df)
        except Exception as e:
            print("Error in main loop:", e)
            traceback.print_exc()
            try:
                send_alert(f"Error: {e}")
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)

# === BACKTEST (Run locally to check profitability) ===
def backtest_strategy():
    """Simulate trades on historical data"""
    df_hist = fetch_data(limit=1000)  # Fetch 1+ year of data
    # Calculate indicators (same as analyze)
    df_hist["EMA"] = ta.ema(df_hist["close"], length=EMA_LENGTH)
    df_hist["RSI"] = ta.rsi(df_hist["close"], length=RSI_LENGTH)
    macd = ta.macd(df_hist["close"], fast=MACD_FAST, slow=MACD_SLOW, signal=MACD_SIGNAL)
    df_hist["MACD_HIST"] = macd[f"MACDh_{MACD_FAST}_{MACD_SLOW}_{MACD_SIGNAL}"]
    adx = ta.adx(df_hist["high"], df_hist["low"], df_hist["close"], length=ADX_LENGTH)
    df_hist["ADX"] = adx[f"ADX_{ADX_LENGTH}"]
    df_hist["ATR"] = ta.atr(df_hist["high"], df_hist["low"], df_hist["close"], length=ATR_LENGTH)
    df_hist["VOL_MA"] = df_hist["volume"].rolling(VOL_MA_LENGTH).mean()

    start_capital = AVAILABLE_CAPITAL
    capital = start_capital
    position = 0.0  # SUI amount
    trades = []
    wins = 0

    for i in range(len(df_hist)):
        row = df_hist.iloc[i]

        if pd.isna(row["RSI"]) or pd.isna(row["MACD_HIST"]) or pd.isna(row["ADX"]):  # Skip early rows
            continue

        # Parabolic check
        if i >= 10:
            recent_closes = df_hist["close"].iloc[i-9:i+1]
            price_change = (row["close"] - recent_closes.iloc[0]) / recent_closes.iloc[0]
            is_parabolic = abs(price_change) > ATR_MULTIPLIER * (row["ATR"] / row["close"])
        else:
            is_parabolic = False

        # Emergency sell
        if position > 0 and row["RSI"] >= EMERGENCY_RSI:
            sell_price = row["close"]
            capital = position * sell_price
            profit = capital - start_capital if trades else 0  # Simplified
            trades.append({"type": "emergency_sell", "price": sell_price, "profit": profit})
            if profit > 0: wins += 1
            position = 0
            continue

        # Buy
        if position == 0 and row["RSI"] < BUY_RSI and row["close"] > row["EMA"] and row["MACD_HIST"] > 0 and row["ADX"] < ADX_MAX and row["volume"] > VOL_MULTIPLIER * row["VOL_MA"] and not is_parabolic:
            buy_price = row["close"]
            position = capital / buy_price  # All-in
            capital = 0
            trades.append({"type": "buy", "price": buy_price})

        # Sell
        elif position > 0 and row["RSI"] > SELL_RSI and row["MACD_HIST"] < 0 and row["ADX"] < ADX_MAX and row["volume"] > VOL_MULTIPLIER * row["VOL_MA"]:
            sell_price = row["close"]
            capital = position * sell_price
            profit = (sell_price - trades[-1]["price"]) / trades[-1]["price"] * 100  # % profit
            trades.append({"type": "sell", "price": sell_price, "profit": profit})
            if profit > 0: wins += 1
            position = 0

    # Close any open position
    if position > 0:
        final_price = df_hist["close"].iloc[-1]
        capital = position * final_price
        profit = (final_price - trades[-1]["price"]) / trades[-1]["price"] * 100
        trades.append({"type": "final_sell", "price": final_price, "profit": profit})
        if profit > 0: wins += 1

    final_capital = capital
    num_trades = len([t for t in trades if t["type"] in ["sell", "emergency_sell", "final_sell"]])
    win_rate = (wins / num_trades * 100) if num_trades > 0 else 0
    total_return = (final_capital - start_capital) / start_capital * 100

    print(f"Backtest Results:")
    print(f"Start Capital: EUR{start_capital:.2f}")
    print(f"Final Capital: EUR{final_capital:.2f}")
    print(f"Total Return: {total_return:.2f}%")
    print(f"Trades: {num_trades} | Win Rate: {win_rate:.2f}%")
    print(f"Trades details: {trades}")

# Uncomment to run backtest
# backtest_strategy()