import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import math
import traceback

# === IMPROVED CONFIG WITH CRASH PROTECTION ===
SYMBOL = "SUI/USDT"
TIMEFRAME = "4h"
EMA_LENGTH = 50
RSI_LENGTH = 14

# EUR/USD exchange rate (will be fetched automatically)
EUR_USD_RATE = None

# NOTE: rotate this webhook if it's public
DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"

CHECK_INTERVAL = 60 * 15  # 15 minutes

# Capital settings
START_FUNDS = 409.64
AUTO_EXECUTE = False  # Keep as paper trading for now!

# EXTREME RSI STRATEGY - All-in/All-out at key levels
BUY_ZONE_EXTREME = 30      # 100% all-in zone
BUY_ZONE_SCALE = 45        # Gradual scaling zone (30-45)
SELL_ZONE_SCALE = 55       # Start scaling out zone (55-70)
SELL_ZONE_EXTREME = 70     # 100% fully out zone
TP1 = 0.06                 # 6% - keep for additional exits
TP2 = 0.15                 # 15% - keep for additional exits

# 🛡️ CRASH PROTECTION FEATURES
ENABLE_BUBBLE_PROTECTION = True
RSI_EXTREME_THRESHOLD = 80      # Extreme overbought - sell everything
PARABOLIC_PROTECTION = True
MAX_POSITION_SIZE = 0.15        # Only applies to momentum buys now!
DAILY_LOSS_LIMIT = 0.08         # Stop trading if down 8% in 24h
MAX_DRAWDOWN_LIMIT = 0.25       # Emergency exit if down 25% from peak

# Position sizing for extreme RSI strategy
SCALE_BUY = {
    "extreme": 1.0,
    "scale_heavy": 0.25,
    "scale_medium": 0.15,
    "scale_light": 0.10
}

SCALE_SELL = {
    "TP1": 0.20,
    "TP2": 0.25,
    "scale_light": 0.15,
    "scale_medium": 0.25,
    "scale_heavy": 0.40,
    "extreme": 1.0
}

EMA_SLOPE_LOOKBACK = 3

# === STATE TRACKING ===
balance_eur = 0.0  # All funds deployed in EUR (paper-mode balance)
in_position = True
entry_price_eur = 409.64 / 142.2382  # initial historic entry
position_qty = 142.2382
entry_time = time.time()  # Set to current time
daily_pnl = 0.0
daily_reset_time = time.time()
peak_portfolio_value = START_FUNDS
last_trade_candle = None
last_alert_rsi_level = None  # Track last RSI alert to avoid spam

# Create exchange client once (reuse)
exchange = ccxt.binance({
    "enableRateLimit": True,
})

def get_eur_usd_rate():
    """Fetch current EUR/USD exchange rate"""
    try:
        # Public free API - fallback supported
        response = requests.get("https://api.exchangerate-api.com/v4/latest/EUR", timeout=10)
        data = response.json()
        # expect data['rates']['USD']
        rate = data.get("rates", {}).get("USD")
        if rate:
            return float(rate)
        # fallback to alternate provider
        response = requests.get("https://open.er-api.com/v6/latest/EUR", timeout=10)
        data = response.json()
        rate = data.get("rates", {}).get("USD")
        if rate:
            return float(rate)
    except Exception as e:
        print(f"Failed to fetch EUR/USD rate: {e}")
        # fallback
    return 1.10

def usd_to_eur(usd_amount):
    """Convert USD to EUR"""
    global EUR_USD_RATE
    if EUR_USD_RATE is None:
        EUR_USD_RATE = get_eur_usd_rate()
    try:
        return float(usd_amount) / float(EUR_USD_RATE)
    except Exception:
        return float(usd_amount)

def eur_to_usd(eur_amount):
    """Convert EUR to USD"""
    global EUR_USD_RATE
    if EUR_USD_RATE is None:
        EUR_USD_RATE = get_eur_usd_rate()
    try:
        return float(eur_amount) * float(EUR_USD_RATE)
    except Exception:
        return float(eur_amount)

def send_alert(message):
    tag = "[SUI CRASH-PROTECTED]"
    full_msg = f"{tag} {message}"
    print(full_msg)
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": full_msg}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def fetch_data():
    """Fetch OHLCV and convert prices from USD/USDT to EUR"""
    global EUR_USD_RATE
    try:
        candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    except Exception as e:
        raise RuntimeError(f"Failed to fetch OHLCV from exchange: {e}")

    df = pd.DataFrame(candles, columns=["time", "open", "high", "low", "close", "volume"])
    # ensure numeric types
    for c in ["open", "high", "low", "close", "volume"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    # normalize time (ms -> int)
    df["time"] = df["time"].astype(int)

    # Convert all prices to EUR (USDT ≈ USD)
    if EUR_USD_RATE is None:
        EUR_USD_RATE = get_eur_usd_rate()
        send_alert(f"💱 EUR/USD rate: {EUR_USD_RATE:.4f}")

    df["open"] = df["open"].apply(usd_to_eur)
    df["high"] = df["high"].apply(usd_to_eur)
    df["low"] = df["low"].apply(usd_to_eur)
    df["close"] = df["close"].apply(usd_to_eur)

    return df

def detect_parabolic_move(df):
    """Detect if we're in a dangerous parabolic move"""
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
        send_alert(f"🚨 PARABOLIC MOVE DETECTED! Price up {price_change*100:.1f}% in 10 candles")

    return is_parabolic

def check_daily_limits():
    """Check if we've hit daily loss limits"""
    global daily_pnl, daily_reset_time

    current_time = time.time()

    # Reset daily P&L every 24 hours
    if current_time - daily_reset_time > 86400:  # 24 hours
        daily_pnl = 0.0
        daily_reset_time = current_time
        send_alert("📅 Daily P&L reset")

    daily_loss_pct = daily_pnl / START_FUNDS if START_FUNDS else 0.0
    if daily_loss_pct < -DAILY_LOSS_LIMIT:
        send_alert(f"🛑 DAILY LOSS LIMIT HIT: {daily_loss_pct*100:.1f}% (limit: {DAILY_LOSS_LIMIT*100:.1f}%)")
        return True

    return False

def check_drawdown_limit(current_value):
    """Check maximum drawdown from peak"""
    global peak_portfolio_value

    if current_value > peak_portfolio_value:
        peak_portfolio_value = current_value

    current_drawdown = (current_value - peak_portfolio_value) / peak_portfolio_value if peak_portfolio_value else 0.0

    if current_drawdown < -MAX_DRAWDOWN_LIMIT:
        send_alert(f"🚨 MAX DRAWDOWN LIMIT HIT: {current_drawdown*100:.1f}% (limit: {MAX_DRAWDOWN_LIMIT*100:.1f}%)")
        return True

    return False

def get_current_candle_id(df):
    """Get unique ID for current candle to prevent multiple trades"""
    if len(df) == 0:
        return None
    # time is in ms, convert to seconds
    return int(df.iloc[-1]["time"] / 1000)

def get_buy_allocation(rsi, balance_remaining):
    """Calculate buy allocation based on RSI level"""
    if rsi <= BUY_ZONE_EXTREME:
        return balance_remaining, "🔥 EXTREME ALL-IN (RSI ≤30)"
    elif rsi <= 35:
        allocation = balance_remaining * SCALE_BUY["scale_heavy"]
        return allocation, f"📈 Heavy scale-in (RSI {rsi:.1f})"
    elif rsi <= 40:
        allocation = balance_remaining * SCALE_BUY["scale_medium"]
        return allocation, f"📊 Medium scale-in (RSI {rsi:.1f})"
    elif rsi <= BUY_ZONE_SCALE:
        allocation = balance_remaining * SCALE_BUY["scale_light"]
        return allocation, f"📉 Light scale-in (RSI {rsi:.1f})"
    else:
        return 0.0, ""

def get_sell_allocation(rsi, position_remaining):
    """Calculate sell allocation based on RSI level"""
    if rsi >= SELL_ZONE_EXTREME:
        return position_remaining, "🚨 EXTREME ALL-OUT (RSI ≥70)"
    elif rsi >= 66:
        allocation = position_remaining * SCALE_SELL["scale_heavy"]
        return allocation, f"📈 Heavy scale-out (RSI {rsi:.1f})"
    elif rsi >= 61:
        allocation = position_remaining * SCALE_SELL["scale_medium"]
        return allocation, f"📊 Medium scale-out (RSI {rsi:.1f})"
    elif rsi >= SELL_ZONE_SCALE:
        allocation = position_remaining * SCALE_SELL["scale_light"]
        return allocation, f"📉 Light scale-out (RSI {rsi:.1f})"
    else:
        return 0.0, ""

def send_rsi_monitoring_alert(rsi, price_eur):
    """Send RSI monitoring alerts for key levels"""
    global last_alert_rsi_level

    alert_levels = [75, 70, 65, 60, 55, 45, 40, 35, 30, 25]

    current_alert_level = None
    for level in alert_levels:
        if (level >= 70 and rsi >= level) or (level <= 45 and rsi <= level):
            current_alert_level = level
            break

    if current_alert_level and current_alert_level != last_alert_rsi_level:
        if current_alert_level >= 70:
            send_alert(f"🚨 RSI EXTREME: {rsi:.1f} @ €{price_eur:.4f} - SELL ZONE ACTIVATED!")
        elif current_alert_level >= 55:
            send_alert(f"⚠️ RSI HIGH: {rsi:.1f} @ €{price_eur:.4f} - Scaling out zone")
        elif current_alert_level <= 30:
            send_alert(f"🔥 RSI EXTREME: {rsi:.1f} @ €{price_eur:.4f} - BUY ZONE ACTIVATED!")
        elif current_alert_level <= 45:
            send_alert(f"👀 RSI LOW: {rsi:.1f} @ €{price_eur:.4f} - Scaling in zone")

        last_alert_rsi_level = current_alert_level

    if 46 <= rsi <= 54:
        last_alert_rsi_level = None

# === IMPROVED TRADE LOGIC ===
def analyze(df):
    global in_position, entry_price_eur, position_qty, balance_eur, entry_time, daily_pnl, last_trade_candle

    # compute indicators
    try:
        df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
        df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)
    except Exception as e:
        raise RuntimeError(f"Indicator calculation failed: {e}")

    # guard against NaN (not enough data)
    if df["EMA"].isnull().all() or df["RSI"].isnull().all():
        print("Not enough data for indicators yet — skipping analyze()")
        return

    price_eur = float(df["close"].iloc[-1])
    ema_eur = float(df["EMA"].iloc[-1])
    rsi = float(df["RSI"].iloc[-1])
    prev_rsi = float(df["RSI"].iloc[-2]) if len(df) > 1 else rsi

    send_rsi_monitoring_alert(rsi, price_eur)

    current_candle = get_current_candle_id(df)

    # current portfolio value
    current_portfolio_value = balance_eur + (position_qty * price_eur)

    # === SAFETY CHECKS ===
    if check_daily_limits():
        if in_position:
            sell_scaled_portion(price_eur, position_qty, "🛑 Daily loss limit - emergency exit")
        return

    if check_drawdown_limit(current_portfolio_value):
        if in_position:
            sell_scaled_portion(price_eur, position_qty, "🚨 Max drawdown limit - emergency exit")
        return

    is_parabolic = False
    if PARABOLIC_PROTECTION:
        try:
            is_parabolic = detect_parabolic_move(df)
        except Exception:
            is_parabolic = False

    if rsi >= RSI_EXTREME_THRESHOLD and in_position:
        sell_scaled_portion(price_eur, position_qty, f"🚨 EXTREME RSI EXIT ({rsi:.1f} ≥ {RSI_EXTREME_THRESHOLD})")
        return

    if len(df) > EMA_SLOPE_LOOKBACK:
        ema_then = df["EMA"].iloc[-1 - EMA_SLOPE_LOOKBACK]
        ema_slope = (ema_eur - ema_then) / ema_then if ema_then and not math.isnan(ema_then) else 0.0
    else:
        ema_slope = 0.0

    # === BUY LOGIC ===
    if current_candle != last_trade_candle and balance_eur > 10.0:
        buy_amount, buy_reason = get_buy_allocation(rsi, balance_eur)
        if buy_amount > 10.0 and not is_parabolic:
            if rsi <= BUY_ZONE_EXTREME or (price_eur > ema_eur and ema_slope > 0):
                execute_scaled_buy(price_eur, rsi, buy_amount, buy_reason)
                last_trade_candle = current_candle

    # === SELL LOGIC ===
    if in_position and entry_price_eur and position_qty > 0:
        sell_amount, sell_reason = get_sell_allocation(rsi, position_qty)
        if sell_amount > 0.001:
            sell_scaled_portion(price_eur, sell_amount, sell_reason)
            last_trade_candle = current_candle
        elif current_candle != last_trade_candle:
            tp1_price = entry_price_eur * (1 + TP1)
            tp2_price = entry_price_eur * (1 + TP2)
            if price_eur >= tp1_price:
                sell_amount = position_qty * SCALE_SELL["TP1"]
                sell_scaled_portion(price_eur, sell_amount, f"🎯 TP1 reached +{TP1*100:.0f}%")
                last_trade_candle = current_candle
            elif price_eur >= tp2_price:
                sell_amount = position_qty * SCALE_SELL["TP2"]
                sell_scaled_portion(price_eur, sell_amount, f"🎯 TP2 reached +{TP2*100:.0f}%")
                last_trade_candle = current_candle

def execute_scaled_buy(price_eur, rsi, eur_amount, reason):
    global in_position, entry_price_eur, position_qty, balance_eur, entry_time

    if eur_amount <= 10:
        return

    qty = eur_amount / price_eur

    if AUTO_EXECUTE:
        balance_eur -= eur_amount

    if in_position:
        # average in
        new_total_value = (entry_price_eur * position_qty) + (price_eur * qty)
        new_total_qty = position_qty + qty
        entry_price_eur = new_total_value / new_total_qty
        position_qty = new_total_qty
    else:
        entry_price_eur = price_eur
        position_qty = qty
        in_position = True
        entry_time = time.time()

    tp1_price = entry_price_eur * (1 + TP1)
    tp2_price = entry_price_eur * (1 + TP2)

    msg = [
        f"✅ BUY — {reason}",
        f"💰 Price: €{price_eur:.4f} | RSI: {rsi:.1f}",
        f"💵 Size: €{eur_amount:.2f} → {qty:.4f} SUI",
        f"🎯 Targets: TP1 €{tp1_price:.4f} (+6%) | TP2 €{tp2_price:.4f} (+15%)",
        f"🛡️ RSI Exits: Scale 55-70, ALL-OUT ≥70",
        f"📊 Position: {position_qty:.4f} SUI @ €{entry_price_eur:.4f}",
        f"💰 Remaining: €{balance_eur:.2f}"
    ]
    send_alert("\n".join(msg))

def sell_scaled_portion(price_eur, qty_to_sell, reason):
    global in_position, position_qty, balance_eur, entry_price_eur, entry_time, daily_pnl

    if qty_to_sell <= 0:
        return

    proceeds = qty_to_sell * price_eur
    if AUTO_EXECUTE:
        balance_eur += proceeds

    pnl = (price_eur - entry_price_eur) * qty_to_sell if entry_price_eur else 0.0
    pnl_pct = (price_eur - entry_price_eur) / entry_price_eur * 100 if entry_price_eur else 0.0

    daily_pnl += pnl

    position_qty -= qty_to_sell
    if position_qty <= 0.001:
        in_position = False
        entry_price_eur = None
        entry_time = None
        position_qty = 0.0

    hours_held = (time.time() - entry_time) / 3600 if entry_time else 0.0

    msg = [
        f"💸 SELL — {reason}",
        f"💰 Price: €{price_eur:.4f}",
        f"📦 Sold: {qty_to_sell:.4f} SUI → €{proceeds:.2f}",
        f"📈 PnL: €{pnl:.2f} ({pnl_pct:+.1f}%) | Held: {hours_held:.1f}h",
        f"📊 Remaining: {position_qty:.4f} SUI | Daily P&L: €{daily_pnl:.2f}",
        f"💰 Balance: €{balance_eur:.2f}"
    ]

    if not AUTO_EXECUTE:
        msg.append("(📝 Paper mode)")

    send_alert("\n".join(msg))

# === MAIN LOOP ===
if __name__ == "__main__":
    startup_msg = [
        f"🛡️ EXTREME RSI STRATEGY BOT STARTED (EUR VERSION)",
        f"📊 CURRENT POSITION: {position_qty:.4f} SUI @ €{entry_price_eur:.4f}",
        f"💰 Capital: €{START_FUNDS:.2f} DEPLOYED | Balance: €{balance_eur:.2f} | Mode: {'LIVE' if AUTO_EXECUTE else 'PAPER'}",
        f"💱 All prices converted to EUR automatically",
        f"🎯 CORE STRATEGY: ALL-IN ≤30 RSI, ALL-OUT ≥70 RSI",
        f"📈 Buy scaling: 100% ≤30, 25% @31-35, 15% @36-40, 10% @41-45",
        f"📉 Sell scaling: 15% @55-60, 25% @61-65, 40% @66-69, 100% ≥70",
        f"🛡️ Protection: Daily -8% limit, Max -25% drawdown, Parabolic detection",
        f"🚨 Emergency: RSI ≥80 (full exit)",
        f"🔄 Monitoring every {CHECK_INTERVAL/60:.0f} minutes"
    ]
    send_alert("\n".join(startup_msg))

    while True:
        try:
            df = fetch_data()
            analyze(df)
        except Exception as e:
            # print full traceback to logs for debugging
            print("Unhandled error in main loop:", e)
            traceback.print_exc()
            try:
                send_alert(f"❌ Error: {e}")
            except Exception:
                pass
        time.sleep(CHECK_INTERVAL)
