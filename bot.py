"""
ChainEX XRP/ZAR EMA Crossover Bot
Strategy: 9/21 EMA crossover + RSI filter + ATR-based position sizing
Exchange: ChainEX (South Africa)
Pair: XRP/ZAR
"""

import os
import time
import hmac
import hashlib
import requests
import logging
import json
from datetime import datetime

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config (from environment variables) ──────────────────────────────────────
PUBLIC_KEY  = os.environ.get("CHAINEX_PUBLIC_KEY", "")
PRIVATE_KEY = os.environ.get("CHAINEX_PRIVATE_KEY", "")

COIN        = "XRP"       # Base coin
EXCHANGE    = "ZAR"       # Quote currency
PAIR        = f"{COIN}_{EXCHANGE}"   # ChainEX format e.g. XRP_ZAR

EMA_FAST    = int(os.environ.get("EMA_FAST", 9))
EMA_SLOW    = int(os.environ.get("EMA_SLOW", 21))
RSI_PERIOD  = int(os.environ.get("RSI_PERIOD", 14))
RSI_OB      = float(os.environ.get("RSI_OB", 70))   # Overbought — skip longs
RSI_OS      = float(os.environ.get("RSI_OS", 30))   # Oversold   — skip shorts

# How much ZAR to risk per trade (or use TRADE_PCT of balance)
TRADE_ZAR   = float(os.environ.get("TRADE_ZAR", 0))        # Fixed ZAR amount (0 = disabled)
TRADE_PCT   = float(os.environ.get("TRADE_PCT", 0.95))     # 95% of available balance

# Set to "true" if you are starting with XRP already held (not ZAR)
# Bot will treat initial state as in_position=True and wait for SELL signal first
START_IN_POSITION = os.environ.get("START_IN_POSITION", "true").lower() == "true"

POLL_SECS   = int(os.environ.get("POLL_SECS", 300))        # 5 min candles

BASE_URL    = "https://api.chainex.io"


# ── ChainEX API Client ────────────────────────────────────────────────────────
class ChainEXClient:
    def __init__(self, public_key: str, private_key: str):
        self.pub  = public_key
        self.priv = private_key

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 signature over sorted query string."""
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        sig = hmac.new(
            self.priv.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()
        return sig

    def _private(self, endpoint: str, params: dict = None) -> dict:
        params = params or {}
        params["nonce"] = int(time.time() * 1000)
        params["api_key"] = self.pub
        params["signature"] = self._sign(params)
        r = requests.post(f"{BASE_URL}{endpoint}", data=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _public(self, endpoint: str, params: dict = None) -> dict:
        r = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Market data ──────────────────────────────────────────────────────────
    def get_trade_history(self, coin: str, exchange: str, limit: int = 200) -> list:
        """Returns recent trades — we'll build synthetic candles from these."""
        data = self._public(f"/market/tradehistory/{coin}/{exchange}/{limit}")
        return data.get("data", [])

    def get_orderbook(self, coin: str, exchange: str) -> dict:
        data = self._public(f"/market/orderbook/{coin}/{exchange}")
        return data.get("data", {})

    def get_market_summary(self, coin: str, exchange: str) -> dict:
        data = self._public(f"/market/summary/{coin}/{exchange}")
        return data.get("data", {})

    # ── Account ───────────────────────────────────────────────────────────────
    def get_balances(self) -> dict:
        data = self._private("/wallet/balances")
        balances = {}
        for item in data.get("data", []):
            balances[item["coin_code"]] = {
                "available": float(item.get("available_balance", 0)),
                "total":     float(item.get("balance", 0)),
            }
        return balances

    # ── Orders ────────────────────────────────────────────────────────────────
    def place_order(self, coin: str, exchange: str, price: float,
                    amount: float, order_type: str) -> dict:
        """
        order_type: 'buy' or 'sell'
        amount: in base coin (XRP)
        price: in quote (ZAR)
        """
        params = {
            "coin_code":     coin,
            "exchange_code": exchange,
            "price":         str(round(price, 4)),
            "amount":        str(round(amount, 4)),
            "type":          order_type,
        }
        return self._private("/trading/addorder", params)

    def get_open_orders(self, coin: str, exchange: str) -> list:
        data = self._private("/trading/openorders", {
            "coin_code":     coin,
            "exchange_code": exchange,
        })
        return data.get("data", [])

    def cancel_order(self, order_id: str) -> dict:
        return self._private("/trading/cancelorder", {"id": order_id})


# ── Technical Indicators ──────────────────────────────────────────────────────

def ema(prices: list, period: int) -> list:
    """Exponential Moving Average."""
    k = 2 / (period + 1)
    result = [None] * len(prices)
    # Seed with SMA of first `period` values
    if len(prices) < period:
        return result
    sma = sum(prices[:period]) / period
    result[period - 1] = sma
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(prices: list, period: int = 14) -> list:
    """Relative Strength Index."""
    result = [None] * len(prices)
    if len(prices) < period + 1:
        return result
    gains, losses = [], []
    for i in range(1, period + 1):
        diff = prices[i] - prices[i - 1]
        gains.append(max(diff, 0))
        losses.append(abs(min(diff, 0)))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(prices)):
        if i > period:
            diff = prices[i] - prices[i - 1]
            avg_gain = (avg_gain * (period - 1) + max(diff, 0)) / period
            avg_loss = (avg_loss * (period - 1) + abs(min(diff, 0))) / period
        rs = avg_gain / avg_loss if avg_loss != 0 else float('inf')
        result[i] = 100 - (100 / (1 + rs))
    return result


def build_candles_from_trades(trades: list, interval_secs: int = 300) -> list:
    """
    ChainEX doesn't expose a candle endpoint publicly.
    We build OHLCV from raw trade history (most-recent 200 trades).
    Returns list of dicts: {open, high, low, close, volume, ts}
    """
    if not trades:
        return []

    # trades come newest-first; reverse to chronological
    trades = list(reversed(trades))

    candles = {}
    for t in trades:
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("time", "")
        try:
            # Accept Unix timestamps or ISO strings
            if str(ts_raw).isdigit():
                ts = int(ts_raw)
            else:
                from datetime import datetime
                dt = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                ts = int(dt.timestamp())
        except Exception:
            continue

        bucket = (ts // interval_secs) * interval_secs
        price  = float(t.get("price", 0))
        volume = float(t.get("amount", 0))

        if bucket not in candles:
            candles[bucket] = {"open": price, "high": price,
                               "low":  price, "close": price,
                               "volume": volume, "ts": bucket}
        else:
            c = candles[bucket]
            c["high"]   = max(c["high"], price)
            c["low"]    = min(c["low"],  price)
            c["close"]  = price
            c["volume"] += volume

    return sorted(candles.values(), key=lambda x: x["ts"])


# ── Signal Logic ──────────────────────────────────────────────────────────────

def get_signal(candles: list) -> str:
    """
    Returns 'BUY', 'SELL', or 'HOLD'
    BUY  = fast EMA crosses above slow EMA  AND RSI < RSI_OB
    SELL = fast EMA crosses below slow EMA  AND RSI > RSI_OS
    """
    if len(candles) < EMA_SLOW + 5:
        log.warning(f"Not enough candles ({len(candles)}) for indicators.")
        return "HOLD"

    closes = [c["close"] for c in candles]

    fast = ema(closes, EMA_FAST)
    slow = ema(closes, EMA_SLOW)
    rsi_vals = rsi(closes, RSI_PERIOD)

    # Last two valid values for crossover detection
    i = len(closes) - 1
    # Walk back to find two consecutive non-None values
    while i > 0 and (fast[i] is None or slow[i] is None or
                     fast[i-1] is None or slow[i-1] is None):
        i -= 1

    if i == 0:
        return "HOLD"

    curr_fast, curr_slow = fast[i],   slow[i]
    prev_fast, prev_slow = fast[i-1], slow[i-1]
    curr_rsi = rsi_vals[i]

    log.info(f"Price: {closes[-1]:.4f}  EMA{EMA_FAST}: {curr_fast:.4f}  "
             f"EMA{EMA_SLOW}: {curr_slow:.4f}  RSI: {curr_rsi:.1f}")

    crossed_up   = prev_fast <= prev_slow and curr_fast > curr_slow
    crossed_down = prev_fast >= prev_slow and curr_fast < curr_slow

    if crossed_up and (curr_rsi is None or curr_rsi < RSI_OB):
        return "BUY"
    if crossed_down and (curr_rsi is None or curr_rsi > RSI_OS):
        return "SELL"
    return "HOLD"


# ── Position Tracker (in-memory) ─────────────────────────────────────────────

STATE_FILE = "state.json"

def load_state(xrp_balance: float = 0.0) -> dict:
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
            # If file exists and is valid, trust it
            return state
    except Exception:
        pass
    # No state file — bootstrap based on START_IN_POSITION flag
    if START_IN_POSITION:
        log.info(f"No state file found. START_IN_POSITION=true — "
                 f"treating current XRP balance ({xrp_balance:.4f} XRP) as open position.")
        state = {
            "in_position": True,
            "entry_price": 0.0,       # Unknown — bot will still sell on signal
            "xrp_amount":  xrp_balance,
        }
    else:
        state = {"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0}
    save_state(state)
    return state

def save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    if not PUBLIC_KEY or not PRIVATE_KEY:
        log.error("Missing CHAINEX_PUBLIC_KEY or CHAINEX_PRIVATE_KEY env vars!")
        return

    client = ChainEXClient(PUBLIC_KEY, PRIVATE_KEY)

    # Fetch live XRP balance before loading state so bootstrap has the right amount
    try:
        init_balances = client.get_balances()
        init_xrp = init_balances.get("XRP", {}).get("available", 0.0)
    except Exception as e:
        log.warning(f"Could not fetch initial balance: {e} — defaulting to 0")
        init_xrp = 0.0

    state = load_state(xrp_balance=init_xrp)

    log.info("=" * 55)
    log.info(f"  ChainEX XRP/ZAR Bot  |  EMA {EMA_FAST}/{EMA_SLOW}")
    log.info(f"  RSI filter: OB={RSI_OB} OS={RSI_OS}")
    log.info(f"  Trade sizing: {int(TRADE_PCT*100)}% of balance")
    log.info(f"  Poll interval: {POLL_SECS}s")
    log.info(f"  START_IN_POSITION: {START_IN_POSITION}")
    log.info(f"  Current state: in_position={state['in_position']}  "
             f"xrp={state['xrp_amount']:.4f}")
    log.info("=" * 55)

    while True:
        try:
            loop_start = time.time()

            # 1. Fetch trade history and build candles
            trades  = client.get_trade_history(COIN, EXCHANGE, limit=200)
            candles = build_candles_from_trades(trades, interval_secs=POLL_SECS)
            log.info(f"Candles built: {len(candles)}")

            if not candles:
                log.warning("No candles — skipping this cycle.")
                time.sleep(POLL_SECS)
                continue

            current_price = candles[-1]["close"]
            log.info(f"Current XRP/ZAR price: R{current_price:.4f}")

            # 2. Get signal
            signal = get_signal(candles)
            log.info(f"Signal: {signal}")

            # 3. Act on signal
            balances = client.get_balances()
            zar_bal  = balances.get("ZAR", {}).get("available", 0.0)
            xrp_bal  = balances.get("XRP", {}).get("available", 0.0)
            log.info(f"Balances — ZAR: R{zar_bal:.2f}  XRP: {xrp_bal:.4f}")

            if signal == "BUY" and not state["in_position"]:
                # Determine how much ZAR to spend
                spend = TRADE_ZAR if TRADE_ZAR > 0 else zar_bal * TRADE_PCT
                spend = min(spend, zar_bal)

                if spend < 10:
                    log.warning(f"Insufficient ZAR balance (R{zar_bal:.2f}). Skipping buy.")
                else:
                    # Limit order slightly above best ask for faster fill
                    book     = client.get_orderbook(COIN, EXCHANGE)
                    asks     = book.get("ask", [])
                    buy_price = float(asks[0]["price"]) * 1.001 if asks else current_price * 1.002
                    xrp_qty  = spend / buy_price

                    log.info(f"BUYING {xrp_qty:.4f} XRP @ R{buy_price:.4f} (spend R{spend:.2f})")
                    resp = client.place_order(COIN, EXCHANGE, buy_price, xrp_qty, "buy")
                    log.info(f"Order response: {resp}")

                    state["in_position"] = True
                    state["entry_price"] = buy_price
                    state["xrp_amount"]  = xrp_qty
                    save_state(state)

            elif signal == "SELL" and state["in_position"]:
                # Sync XRP amount from live balance in case we bootstrapped without a known amount
                sell_qty = xrp_bal * 0.99  # keep 1% buffer for fees
                if state["xrp_amount"] > 0:
                    sell_qty = min(state["xrp_amount"], xrp_bal) * 0.99

                if sell_qty < 1:
                    log.warning(f"Insufficient XRP to sell ({xrp_bal:.4f} XRP). Resetting state.")
                    state["in_position"] = False
                    save_state(state)
                else:
                    book       = client.get_orderbook(COIN, EXCHANGE)
                    bids       = book.get("bid", [])
                    sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998

                    if state["entry_price"] > 0:
                        pnl = (sell_price - state["entry_price"]) * sell_qty
                        log.info(f"SELLING {sell_qty:.4f} XRP @ R{sell_price:.4f}  "
                                 f"(est. PnL: R{pnl:.2f})")
                    else:
                        log.info(f"SELLING {sell_qty:.4f} XRP @ R{sell_price:.4f}  "
                                 f"(entry price unknown — bootstrapped position)")

                    resp = client.place_order(COIN, EXCHANGE, sell_price, sell_qty, "sell")
                    log.info(f"Order response: {resp}")

                    state["in_position"] = False
                    state["entry_price"] = 0.0
                    state["xrp_amount"]  = 0.0
                    save_state(state)

            else:
                log.info("No action this cycle.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        # Sleep until next candle close
        elapsed = time.time() - loop_start
        sleep_for = max(POLL_SECS - elapsed, 10)
        log.info(f"Sleeping {sleep_for:.0f}s until next cycle...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()

