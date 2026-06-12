"""
ChainEX XRP/ZAR EMA Crossover Bot  (v3 — fixed API auth)
Strategy: 9/21 EMA crossover + RSI filter
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

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
PUBLIC_KEY  = os.environ.get("CHAINEX_PUBLIC_KEY", "")
PRIVATE_KEY = os.environ.get("CHAINEX_PRIVATE_KEY", "")

COIN        = "XRP"
EXCHANGE    = "ZAR"

EMA_FAST    = int(os.environ.get("EMA_FAST", 9))
EMA_SLOW    = int(os.environ.get("EMA_SLOW", 21))
RSI_PERIOD  = int(os.environ.get("RSI_PERIOD", 14))
RSI_OB      = float(os.environ.get("RSI_OB", 70))
RSI_OS      = float(os.environ.get("RSI_OS", 30))

TRADE_ZAR   = float(os.environ.get("TRADE_ZAR", 0))
TRADE_PCT   = float(os.environ.get("TRADE_PCT", 0.95))

START_IN_POSITION = os.environ.get("START_IN_POSITION", "true").lower() == "true"
POLL_SECS   = int(os.environ.get("POLL_SECS", 300))

BASE_URL    = "https://api.chainex.io"


# ── ChainEX API Client ────────────────────────────────────────────────────────
class ChainEXClient:
    def __init__(self, public_key: str, private_key: str):
        self.pub  = public_key
        self.priv = private_key

    def _sign(self, params: dict) -> str:
        """HMAC-SHA256 over alphabetically sorted key=value pairs."""
        payload = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return hmac.new(
            self.priv.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def _private_get(self, endpoint: str, extra: dict = None) -> dict:
        """Authenticated GET request — params passed as query string."""
        params = extra.copy() if extra else {}
        params["nonce"]     = int(time.time() * 1000)
        params["api_key"]   = self.pub
        params["signature"] = self._sign(params)
        r = requests.get(f"{BASE_URL}{endpoint}", params=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _private_post(self, endpoint: str, extra: dict = None) -> dict:
        """Authenticated POST request — params in form body."""
        params = extra.copy() if extra else {}
        params["nonce"]     = int(time.time() * 1000)
        params["api_key"]   = self.pub
        params["signature"] = self._sign(params)
        r = requests.post(f"{BASE_URL}{endpoint}", data=params, timeout=10)
        r.raise_for_status()
        return r.json()

    def _public(self, endpoint: str) -> dict:
        r = requests.get(f"{BASE_URL}{endpoint}", timeout=10)
        r.raise_for_status()
        return r.json()

    # ── Market data ──────────────────────────────────────────────────────────
    def get_trade_history(self, coin: str, exchange: str, limit: int = 200) -> list:
        data = self._public(f"/market/tradehistory/{coin}/{exchange}/{limit}")
        return data.get("data", [])

    def get_orderbook(self, coin: str, exchange: str) -> dict:
        data = self._public(f"/market/orderbook/{coin}/{exchange}")
        return data.get("data", {})

    # ── Account (try GET first, fall back to POST) ────────────────────────────
    def get_balances(self) -> dict:
        # Try GET endpoint first
        for method in ["get", "post"]:
            try:
                if method == "get":
                    data = self._private_get("/wallet/balances")
                else:
                    data = self._private_post("/wallet/balances")

                raw = data.get("data", [])
                if raw is None:
                    raw = []

                # Handle both list and dict responses
                if isinstance(raw, dict):
                    raw = list(raw.values())

                balances = {}
                for item in raw:
                    if isinstance(item, dict):
                        code = item.get("coin_code") or item.get("code") or item.get("currency")
                        if code:
                            balances[code.upper()] = {
                                "available": float(item.get("available_balance",
                                                   item.get("available", 0))),
                                "total":     float(item.get("balance",
                                                   item.get("total", 0))),
                            }
                log.info(f"Balances fetched via {method.upper()}: {list(balances.keys())}")
                return balances

            except Exception as e:
                log.warning(f"Balance fetch via {method.upper()} failed: {e}")
                time.sleep(1)

        log.error("Could not fetch balances via GET or POST.")
        return {}

    # ── Orders ────────────────────────────────────────────────────────────────
    def place_order(self, coin: str, exchange: str, price: float,
                    amount: float, order_type: str) -> dict:
        params = {
            "coin_code":     coin,
            "exchange_code": exchange,
            "price":         str(round(price, 4)),
            "amount":        str(round(amount, 4)),
            "type":          order_type,
        }
        # Try POST first (most common for order placement)
        try:
            return self._private_post("/trading/addorder", params)
        except Exception as e:
            log.warning(f"POST order failed: {e}, trying GET...")
            return self._private_get("/trading/addorder", params)


# ── Indicators ────────────────────────────────────────────────────────────────

def ema(prices, period):
    k = 2 / (period + 1)
    result = [None] * len(prices)
    if len(prices) < period:
        return result
    result[period - 1] = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result


def rsi(prices, period=14):
    result = [None] * len(prices)
    if len(prices) < period + 1:
        return result
    gains = [max(prices[i] - prices[i-1], 0) for i in range(1, period + 1)]
    losses = [abs(min(prices[i] - prices[i-1], 0)) for i in range(1, period + 1)]
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    for i in range(period, len(prices)):
        if i > period:
            d = prices[i] - prices[i-1]
            avg_gain = (avg_gain * (period - 1) + max(d, 0)) / period
            avg_loss = (avg_loss * (period - 1) + abs(min(d, 0))) / period
        rs = avg_gain / avg_loss if avg_loss else float('inf')
        result[i] = 100 - (100 / (1 + rs))
    return result


def build_candles(trades, interval_secs=300):
    if not trades:
        return []
    trades = list(reversed(trades))
    candles = {}
    for t in trades:
        ts_raw = t.get("created_at") or t.get("timestamp") or t.get("time", "")
        try:
            ts = int(ts_raw) if str(ts_raw).isdigit() else int(
                __import__("datetime").datetime.fromisoformat(
                    str(ts_raw).replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        bucket = (ts // interval_secs) * interval_secs
        price  = float(t.get("price", 0))
        vol    = float(t.get("amount", 0))
        if bucket not in candles:
            candles[bucket] = {"open": price, "high": price,
                               "low": price, "close": price,
                               "volume": vol, "ts": bucket}
        else:
            c = candles[bucket]
            c["high"]   = max(c["high"], price)
            c["low"]    = min(c["low"], price)
            c["close"]  = price
            c["volume"] += vol
    return sorted(candles.values(), key=lambda x: x["ts"])


def get_signal(candles):
    if len(candles) < EMA_SLOW + 5:
        log.warning(f"Not enough candles ({len(candles)})")
        return "HOLD"
    closes = [c["close"] for c in candles]
    fast   = ema(closes, EMA_FAST)
    slow   = ema(closes, EMA_SLOW)
    rsi_v  = rsi(closes, RSI_PERIOD)

    i = len(closes) - 1
    while i > 0 and any(v is None for v in [fast[i], slow[i], fast[i-1], slow[i-1]]):
        i -= 1
    if i == 0:
        return "HOLD"

    log.info(f"Price: {closes[-1]:.4f}  EMA{EMA_FAST}: {fast[i]:.4f}  "
             f"EMA{EMA_SLOW}: {slow[i]:.4f}  RSI: {rsi_v[i]:.1f if rsi_v[i] else 'N/A'}")

    crossed_up   = fast[i-1] <= slow[i-1] and fast[i] > slow[i]
    crossed_down = fast[i-1] >= slow[i-1] and fast[i] < slow[i]

    if crossed_up   and (rsi_v[i] is None or rsi_v[i] < RSI_OB):  return "BUY"
    if crossed_down and (rsi_v[i] is None or rsi_v[i] > RSI_OS):  return "SELL"
    return "HOLD"


# ── State ─────────────────────────────────────────────────────────────────────

STATE_FILE = "state.json"

def load_state(xrp_balance=0.0):
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        pass
    if START_IN_POSITION:
        log.info(f"Bootstrapping: treating {xrp_balance:.4f} XRP as open position.")
        state = {"in_position": True, "entry_price": 0.0, "xrp_amount": xrp_balance}
    else:
        state = {"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0}
    save_state(state)
    return state

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    if not PUBLIC_KEY or not PRIVATE_KEY:
        log.error("Missing CHAINEX_PUBLIC_KEY or CHAINEX_PRIVATE_KEY!")
        return

    client = ChainEXClient(PUBLIC_KEY, PRIVATE_KEY)

    try:
        init_bal = client.get_balances()
        init_xrp = init_bal.get("XRP", {}).get("available", 0.0)
    except Exception as e:
        log.warning(f"Initial balance fetch failed: {e}")
        init_xrp = 0.0

    state = load_state(xrp_balance=init_xrp)

    log.info("=" * 55)
    log.info(f"  ChainEX XRP/ZAR Bot  |  EMA {EMA_FAST}/{EMA_SLOW}")
    log.info(f"  RSI OB={RSI_OB} OS={RSI_OS}  |  Sizing: {int(TRADE_PCT*100)}%")
    log.info(f"  Poll: {POLL_SECS}s  |  In position: {state['in_position']}")
    log.info(f"  XRP tracked: {state['xrp_amount']:.4f}")
    log.info("=" * 55)

    while True:
        try:
            t0 = time.time()

            trades  = client.get_trade_history(COIN, EXCHANGE, 200)
            candles = build_candles(trades, POLL_SECS)
            log.info(f"Candles: {len(candles)}")

            if not candles:
                log.warning("No candles this cycle.")
                time.sleep(POLL_SECS)
                continue

            current_price = candles[-1]["close"]
            log.info(f"XRP/ZAR: R{current_price:.4f}")

            signal   = get_signal(candles)
            log.info(f"Signal: {signal}")

            balances = client.get_balances()
            zar_bal  = balances.get("ZAR", {}).get("available", 0.0)
            xrp_bal  = balances.get("XRP", {}).get("available", 0.0)
            log.info(f"ZAR: R{zar_bal:.2f}  XRP: {xrp_bal:.4f}")

            if signal == "BUY" and not state["in_position"]:
                spend = TRADE_ZAR if TRADE_ZAR > 0 else zar_bal * TRADE_PCT
                spend = min(spend, zar_bal)
                if spend < 10:
                    log.warning(f"ZAR too low to buy (R{zar_bal:.2f})")
                else:
                    book      = client.get_orderbook(COIN, EXCHANGE)
                    asks      = book.get("ask", [])
                    buy_price = float(asks[0]["price"]) * 1.001 if asks else current_price * 1.002
                    xrp_qty   = spend / buy_price
                    log.info(f"BUY {xrp_qty:.4f} XRP @ R{buy_price:.4f} (R{spend:.2f})")
                    resp = client.place_order(COIN, EXCHANGE, buy_price, xrp_qty, "buy")
                    log.info(f"Order: {resp}")
                    state.update({"in_position": True, "entry_price": buy_price,
                                  "xrp_amount": xrp_qty})
                    save_state(state)

            elif signal == "SELL" and state["in_position"]:
                sell_qty = (min(state["xrp_amount"], xrp_bal) if state["xrp_amount"] > 0
                            else xrp_bal) * 0.99
                if sell_qty < 1:
                    log.warning(f"Not enough XRP to sell ({xrp_bal:.4f}). Resetting.")
                    state.update({"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
                else:
                    book       = client.get_orderbook(COIN, EXCHANGE)
                    bids       = book.get("bid", [])
                    sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998
                    pnl_str    = (f"R{(sell_price - state['entry_price']) * sell_qty:.2f}"
                                  if state["entry_price"] > 0 else "unknown entry")
                    log.info(f"SELL {sell_qty:.4f} XRP @ R{sell_price:.4f} (PnL: {pnl_str})")
                    resp = client.place_order(COIN, EXCHANGE, sell_price, sell_qty, "sell")
                    log.info(f"Order: {resp}")
                    state.update({"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
            else:
                log.info("HOLD — no action.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        sleep_for = max(POLL_SECS - (time.time() - t0), 10)
        log.info(f"Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
