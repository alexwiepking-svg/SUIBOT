import ccxt
import pandas as pd
import pandas_ta as ta
import requests
import time
import math

# === IMPROVED CONFIG WITH CRASH PROTECTION ===
SYMBOL = "SUI/USDT"   
TIMEFRAME = "4h"
EMA_LENGTH = 50
RSI_LENGTH = 14

DISCORD_WEBHOOK = "https://discord.com/api/webhooks/1417188364574265344/6Bd9bfSA83-BsL2ARD5DVOtnQfAHGGrl5ySMH5cEv2aRT2PzfSG2Pr3pZjSj5Eb8VX5l"
CHECK_INTERVAL = 60 * 15  # 15 minutes

# Capital settings
START_FUNDS = 409.64
AUTO_EXECUTE = False  # Keep as paper trading for now!

# CONSERVATIVE SETTINGS (learned from 2025 failure)
BUY_ZONE_DEEP = 30      # Full buy zone
BUY_ZONE_MOMENTUM = 45  # Reduced from 55! More conservative momentum
TP1 = 0.06              # 6% - this worked well
TP2 = 0.15              # 15% - this worked well

# 🛡️ CRASH PROTECTION FEATURES
ENABLE_BUBBLE_PROTECTION = True
RSI_EXTREME_THRESHOLD = 80      # Extreme overbought - sell everything
PARABOLIC_PROTECTION = True
MAX_POSITION_SIZE = 0.15        # Never more than 15% per momentum buy
DAILY_LOSS_LIMIT = 0.08         # Stop trading if down 8% in 24h
MAX_DRAWDOWN_LIMIT = 0.25       # Emergency exit if down 25% from peak

# Position sizing (much more conservative)
SCALE_BUY = {
    "deep": 0.50,           # Reduced from 75% - be more careful
    "momentum": 0.15        # Reduced from 30% - way more conservative
}

SCALE_SELL = {
    "TP1": 0.40,           # Sell more at TP1 (was 25%)
    "TP2": 0.60,           # Sell most at TP2 (was 70%)
    "RSI_WARNING": 0.30,   # New: partial exit at RSI 68
    "RSI_DANGER": 1.0      # Full exit at RSI 75 (not 70!)
}

EMA_SLOPE_LOOKBACK = 3

# === STATE TRACKING ===
balance_usdt = START_FUNDS
in_position = False
entry_price = None
position_qty = 0.0
entry_time = None
daily_pnl = 0.0
daily_reset_time = time.time()
peak_portfolio_value = START_FUNDS
last_trade_candle = None

# === HELPERS ===
def send_alert(message):
    tag = "[SUI CRASH-PROTECTED]"
    full_msg = f"{tag} {message}"
    print(full_msg)
    try:
        requests.post(DISCORD_WEBHOOK, json={"content": full_msg}, timeout=10)
    except Exception as e:
        print(f"Failed to send Discord alert: {e}")

def fetch_data():
    exchange = ccxt.binance()
    candles = exchange.fetch_ohlcv(SYMBOL, timeframe=TIMEFRAME, limit=100)
    df = pd.DataFrame(candles, columns=["time","open","high","low","close","volume"])
    df["close"] = df["close"].astype(float)
    df["high"] = df["high"].astype(float)
    df["low"] = df["low"].astype(float)
    return df

def detect_parabolic_move(df):
    """Detect if we're in a dangerous parabolic move"""
    if len(df) < 10:
        return False
        
    recent_closes = df["close"].tail(10)
    
    # Check for rapid price appreciation (>40% in 10 candles)
    price_change = (recent_closes.iloc[-1] - recent_closes.iloc[0]) / recent_closes.iloc[0]
    
    # Check for accelerating momentum (each period higher than last)
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
    
    # Reset daily P&L at midnight UTC
    if current_time - daily_reset_time > 86400:  # 24 hours
        daily_pnl = 0
        daily_reset_time = current_time
        send_alert("📅 Daily P&L reset")
    
    # Check if we've hit daily loss limit
    daily_loss_pct = daily_pnl / START_FUNDS
    if daily_loss_pct < -DAILY_LOSS_LIMIT:
        send_alert(f"🛑 DAILY LOSS LIMIT HIT: {daily_loss_pct*100:.1f}% (limit: {DAILY_LOSS_LIMIT*100:.1f}%)")
        return True
    
    return False

def check_drawdown_limit(current_value):
    """Check maximum drawdown from peak"""
    global peak_portfolio_value
    
    if current_value > peak_portfolio_value:
        peak_portfolio_value = current_value
    
    current_drawdown = (current_value - peak_portfolio_value) / peak_portfolio_value
    
    if current_drawdown < -MAX_DRAWDOWN_LIMIT:
        send_alert(f"🚨 MAX DRAWDOWN LIMIT HIT: {current_drawdown*100:.1f}% (limit: {MAX_DRAWDOWN_LIMIT*100:.1f}%)")
        return True
    
    return False

def get_current_candle_id(df):
    """Get unique ID for current candle to prevent multiple trades"""
    if len(df) == 0:
        return None
    return int(df.iloc[-1]["time"] / 1000)  # Convert to seconds

# === IMPROVED TRADE LOGIC ===
def analyze(df):
    global in_position, entry_price, position_qty, balance_usdt, entry_time, daily_pnl, last_trade_candle

    df["EMA"] = ta.ema(df["close"], length=EMA_LENGTH)
    df["RSI"] = ta.rsi(df["close"], length=RSI_LENGTH)

    price = df["close"].iloc[-1]
    ema = df["EMA"].iloc[-1]
    rsi = df["RSI"].iloc[-1]
    prev_rsi = df["RSI"].iloc[-2] if len(df) > 1 else rsi

    # Get current candle ID to prevent multiple trades per candle
    current_candle = get_current_candle_id(df)
    
    # Calculate current portfolio value
    current_portfolio_value = balance_usdt + (position_qty * price)
    
    # === SAFETY CHECKS ===
    # 1. Daily loss limit
    if check_daily_limits():
        if in_position:
            sell_portion(price, 1.0, "🛑 Daily loss limit - emergency exit")
        return
    
    # 2. Maximum drawdown limit
    if check_drawdown_limit(current_portfolio_value):
        if in_position:
            sell_portion(price, 1.0, "🚨 Max drawdown limit - emergency exit")
        return
    
    # 3. Detect parabolic moves
    is_parabolic = False
    if PARABOLIC_PROTECTION:
        is_parabolic = detect_parabolic_move(df)
    
    # 4. Extreme RSI protection
    if rsi >= RSI_EXTREME_THRESHOLD and in_position:
        sell_portion(price, 1.0, f"🚨 EXTREME RSI EXIT ({rsi:.1f} ≥ {RSI_EXTREME_THRESHOLD})")
        return

    # EMA slope calculation
    if len(df) > EMA_SLOPE_LOOKBACK:
        ema_then = df["EMA"].iloc[-1 - EMA_SLOPE_LOOKBACK]
        ema_slope = (ema - ema_then) / ema_then if ema_then and not math.isnan(ema_then) else 0.0
    else:
        ema_slope = 0

    # === CONSERVATIVE BUY LOGIC ===
    if current_candle != last_trade_candle:  # One trade per candle
        
        # Full buy in deep oversold (RSI ≤ 30)
        if rsi <= BUY_ZONE_DEEP and not is_parabolic:
            execute_buy(price, rsi, SCALE_BUY["deep"], "🔥 Deep oversold entry (RSI ≤30)")
            last_trade_candle = current_candle
        
        # MUCH more conservative momentum buy (RSI 30-45, not 30-55!)
        elif (BUY_ZONE_DEEP < rsi <= BUY_ZONE_MOMENTUM and 
              price > ema and ema_slope > 0 and not is_parabolic):
            # Additional safety: reduce size if RSI is higher in the range
            size_multiplier = max(0.5, (BUY_ZONE_MOMENTUM - rsi) / (BUY_ZONE_MOMENTUM - BUY_ZONE_DEEP))
            adjusted_size = SCALE_BUY["momentum"] * size_multiplier
            
            execute_buy(price, rsi, adjusted_size, f"📈 Conservative momentum ({rsi:.1f}, size: {adjusted_size*100:.0f}%)")
            last_trade_candle = current_candle

    # === IMPROVED SELL LOGIC ===
    if in_position and entry_price:
        tp1_price = entry_price * (1 + TP1)
        tp2_price = entry_price * (1 + TP2)
        
        # Take Profit 1 (sell more than before)
        if price >= tp1_price and position_qty > 0:
            sell_portion(price, SCALE_SELL["TP1"], f"🎯 TP1 reached +{TP1*100:.0f}%")
            last_trade_candle = current_candle

        # Take Profit 2 (sell most of remaining)
        if price >= tp2_price and position_qty > 0:
            sell_portion(price, SCALE_SELL["TP2"], f"🎯 TP2 reached +{TP2*100:.0f}%")
            last_trade_candle = current_candle

        # RSI Exit Logic (FIXED - no overlapping exits!)
        if rsi >= 75 and position_qty > 0:
            # FULL exit at RSI 75+ (highest priority)
            sell_portion(price, 1.0, f"🚨 RSI full exit ({rsi:.1f} ≥ 75)")
            last_trade_candle = current_candle
        elif rsi >= 68 and position_qty > 0 and current_candle != last_trade_candle:
            # Partial exit at RSI 68-74 (only if not already exited)
            sell_portion(price, SCALE_SELL["RSI_WARNING"], f"⚠️ RSI warning exit ({rsi:.1f} ≥ 68)")
            last_trade_candle = current_candle

def execute_buy(price, rsi, portion, reason):
    global in_position, entry_price, position_qty, balance_usdt, entry_time

    if portion <= 0:
        return

    # Additional position size limits
    max_allowed = min(portion, MAX_POSITION_SIZE)
    usdt_alloc = balance_usdt * max_allowed
    
    if usdt_alloc <= 10:  # Minimum trade size
        return

    qty = usdt_alloc / price

    if AUTO_EXECUTE:
        balance_usdt -= usdt_alloc

    # Position tracking
    if in_position:
        # Average into position
        new_total_value = (entry_price * position_qty) + (price * qty)
        new_total_qty = position_qty + qty
        entry_price = new_total_value / new_total_qty
        position_qty = new_total_qty
    else:
        entry_price = price
        position_qty = qty
        in_position = True
        entry_time = time.time()

    tp1_price = entry_price * (1 + TP1)
    tp2_price = entry_price * (1 + TP2)

    msg = [
        f"✅ BUY — {reason}",
        f"💰 Price: ${price:.4f} | RSI: {rsi:.1f}",
        f"💵 Size: ${usdt_alloc:.2f} ({max_allowed*100:.0f}%) → {qty:.2f} SUI",
        f"🎯 Targets: TP1 ${tp1_price:.4f} (+6%) | TP2 ${tp2_price:.4f} (+15%)",
        f"🛡️ Exits: RSI 68 (30%), RSI 75 (100%), Extreme 80+ (100%)",
        f"📊 Position: {position_qty:.2f} SUI @ ${entry_price:.4f}"
    ]
    send_alert("\n".join(msg))

def sell_portion(price, fraction, reason):
    global in_position, position_qty, balance_usdt, entry_price, entry_time, daily_pnl

    qty_to_sell = position_qty * fraction
    if qty_to_sell <= 0:
        return

    proceeds = qty_to_sell * price
    if AUTO_EXECUTE:
        balance_usdt += proceeds

    pnl = (price - entry_price) * qty_to_sell if entry_price else 0
    pnl_pct = (price - entry_price) / entry_price * 100 if entry_price else 0
    
    # Update daily P&L tracking
    daily_pnl += pnl

    position_qty -= qty_to_sell
    
    if position_qty <= 0.001:
        in_position = False
        entry_price = None
        entry_time = None
        position_qty = 0

    hours_held = (time.time() - entry_time) / 3600 if entry_time else 0

    msg = [
        f"💸 SELL — {reason}",
        f"💰 Price: ${price:.4f}",
        f"📦 Sold: {qty_to_sell:.2f} SUI → ${proceeds:.2f}",
        f"📈 PnL: ${pnl:.2f} ({pnl_pct:+.1f}%) | Held: {hours_held:.1f}h",
        f"📊 Remaining: {position_qty:.2f} SUI | Daily P&L: ${daily_pnl:.2f}"
    ]
    
    if AUTO_EXECUTE:
        msg.append(f"💰 Balance: ${balance_usdt:.2f}")
    else:
        msg.append("(📝 Paper mode)")
        
    send_alert("\n".join(msg))

# === MAIN LOOP ===
if __name__ == "__main__":
    startup_msg = [
        f"🛡️ CRASH-PROTECTED SUI BOT STARTED",
        f"📊 Lessons learned from 2025 failure applied!",
        f"💰 Capital: ${START_FUNDS:.2f} | Mode: {'LIVE' if AUTO_EXECUTE else 'PAPER'}",
        f"🎯 Conservative: Deep buy ≤30 RSI, Momentum ≤45 RSI (was 55)",
        f"🛡️ Protection: Daily -8% limit, Max -25% drawdown, Parabolic detection",
        f"📈 Exits: TP1 6% (40%), TP2 15% (60%), RSI 68 (30%), RSI 75 (100%)",
        f"🚨 Emergency: RSI ≥80 (full exit), Max 15% per momentum trade",
        f"🔄 Monitoring every {CHECK_INTERVAL/60:.0f} minutes"
    ]
    send_alert("\n".join(startup_msg))
    
    while True:
        try:
            df = fetch_data()
            analyze(df)
        except Exception as e:
            send_alert(f"❌ Error: {e}")
            print(f"Error: {e}")
        time.sleep(CHECK_INTERVAL)
