"""
ChainEX XRP/ZAR EMA Crossover Bot  (v4 — correct auth headers + URL format)
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger(__name__)

PUBLIC_KEY  = os.environ.get("CHAINEX_PUBLIC_KEY", "")
PRIVATE_KEY = os.environ.get("CHAINEX_PRIVATE_KEY", "")

COIN     = "XRP"
EXCHANGE = "ZAR"

EMA_FAST = int(os.environ.get("EMA_FAST", 9))
EMA_SLOW = int(os.environ.get("EMA_SLOW", 21))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", 14))
RSI_OB   = float(os.environ.get("RSI_OB", 70))
RSI_OS   = float(os.environ.get("RSI_OS", 30))

TRADE_ZAR = float(os.environ.get("TRADE_ZAR", 0))
TRADE_PCT = float(os.environ.get("TRADE_PCT", 0.95))
START_IN_POSITION = os.environ.get("START_IN_POSITION", "true").lower() == "true"
POLL_SECS = int(os.environ.get("POLL_SECS", 300))

BASE_URL = "https://api.chainex.io"


class ChainEXClient:
    def __init__(self, public_key, private_key):
        self.pub  = public_key
        self.priv = private_key

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self.priv.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def _private(self, endpoint: str, body: dict = None) -> dict:
        """
        ChainEX private API:
        - POST with JSON body
        - Headers: api-key, api-signature (HMAC of JSON body string), api-nonce
        """
        body = body or {}
        nonce = str(int(time.time() * 1000))
        body_str = json.dumps(body, separators=(',', ':')) if body else "{}"
        signature = self._sign(body_str)

        headers = {
            "Content-Type":  "application/json",
            "api-key":       self.pub,
            "api-nonce":     nonce,
            "api-signature": signature,
        }
        r = requests.post(
            f"{BASE_URL}{endpoint}",
            headers=headers,
            data=body_str,
            timeout=10
        )
        log.debug(f"POST {endpoint} → {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    def _public(self, endpoint: str) -> dict:
        r = requests.get(f"{BASE_URL}{endpoint}", timeout=10)
        log.debug(f"GET {endpoint} → {r.status_code}: {r.text[:200]}")
        r.raise_for_status()
        return r.json()

    # ── Market data ──────────────────────────────────────────────────────────
    def get_trade_history(self, coin, exchange, limit=200) -> list:
        # Try both URL formats
        for path in [
            f"/market/tradehistory/{coin}_{exchange}/{limit}",
            f"/market/tradehistory/{coin}/{exchange}/{limit}",
            f"/market/history/{coin}_{exchange}",
        ]:
            try:
                data = self._public(path)
                result = data.get("data", [])
                if result:
                    log.info(f"Trade history OK via {path} ({len(result)} trades)")
                    return result
            except Exception as e:
                log.warning(f"Trade history path {path} failed: {e}")
        return []

    def get_orderbook(self, coin, exchange) -> dict:
        for path in [
            f"/market/orderbook/{coin}_{exchange}",
            f"/market/orderbook/{coin}/{exchange}",
        ]:
            try:
                data = self._public(path)
                result = data.get("data", {})
                if result:
                    return result
            except Exception as e:
                log.warning(f"Orderbook path {path} failed: {e}")
        return {}

    def get_market_summary(self, coin, exchange) -> dict:
        for path in [
            f"/market/summary/{coin}_{exchange}",
            f"/market/summary/{coin}/{exchange}",
        ]:
            try:
                data = self._public(path)
                return data.get("data", {})
            except Exception:
                pass
        return {}

    # ── Account ───────────────────────────────────────────────────────────────
    def get_balances(self) -> dict:
        # Try multiple known endpoint patterns
        for endpoint in ["/wallet/balances", "/account/balances", "/user/balances"]:
            try:
                data = self._private(endpoint)
                raw = data.get("data", [])
                if raw is None:
                    continue
                if isinstance(raw, dict):
                    raw = list(raw.values())
                balances = {}
                for item in raw:
                    if isinstance(item, dict):
                        code = (item.get("coin_code") or item.get("code") or
                                item.get("currency") or "")
                        if code:
                            balances[code.upper()] = {
                                "available": float(item.get("available_balance",
                                             item.get("available", 0))),
                                "total":     float(item.get("balance",
                                             item.get("total", 0))),
                            }
                if balances:
                    log.info(f"Balances via {endpoint}: {list(balances.keys())}")
                    return balances
            except Exception as e:
                log.warning(f"Balance endpoint {endpoint} failed: {e}")
        log.error("All balance endpoints failed.")
        return {}

    # ── Orders ────────────────────────────────────────────────────────────────
    def place_order(self, coin, exchange, price, amount, order_type) -> dict:
        body = {
            "coin_code":     coin,
            "exchange_code": exchange,
            "price":         str(round(price, 4)),
            "amount":        str(round(amount, 4)),
            "type":          order_type,
        }
        for endpoint in ["/trading/addorder", "/order/add", "/trade/order"]:
            try:
                resp = self._private(endpoint, body)
                log.info(f"Order placed via {endpoint}: {resp}")
                return resp
            except Exception as e:
                log.warning(f"Order endpoint {endpoint} failed: {e}")
        return {}


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
    gains  = [max(prices[i] - prices[i-1], 0)       for i in range(1, period + 1)]
    losses = [abs(min(prices[i] - prices[i-1], 0))  for i in range(1, period + 1)]
    ag, al = sum(gains) / period, sum(losses) / period
    for i in range(period, len(prices)):
        if i > period:
            d  = prices[i] - prices[i-1]
            ag = (ag * (period - 1) + max(d, 0))       / period
            al = (al * (period - 1) + abs(min(d, 0)))  / period
        rs = ag / al if al else float('inf')
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
            if str(ts_raw).isdigit():
                ts = int(ts_raw)
            else:
                import datetime
                ts = int(datetime.datetime.fromisoformat(
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
            c["low"]    = min(c["low"],  price)
            c["close"]  = price
            c["volume"] += vol
    return sorted(candles.values(), key=lambda x: x["ts"])

def get_signal(candles):
    if len(candles) < EMA_SLOW + 5:
        log.warning(f"Not enough candles ({len(candles)})")
        return "HOLD"
    closes  = [c["close"] for c in candles]
    fast    = ema(closes, EMA_FAST)
    slow    = ema(closes, EMA_SLOW)
    rsi_v   = rsi(closes, RSI_PERIOD)
    i = len(closes) - 1
    while i > 0 and any(v is None for v in [fast[i], slow[i], fast[i-1], slow[i-1]]):
        i -= 1
    if i == 0:
        return "HOLD"
    log.info(f"Price:{closes[-1]:.4f}  EMA{EMA_FAST}:{fast[i]:.4f}  "
             f"EMA{EMA_SLOW}:{slow[i]:.4f}  RSI:{rsi_v[i]:.1f if rsi_v[i] else 'N/A'}")
    if fast[i-1] <= slow[i-1] and fast[i] > slow[i]:
        if rsi_v[i] is None or rsi_v[i] < RSI_OB:  return "BUY"
    if fast[i-1] >= slow[i-1] and fast[i] < slow[i]:
        if rsi_v[i] is None or rsi_v[i] > RSI_OS:  return "SELL"
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
        log.info(f"Bootstrapping in_position=True with {xrp_balance:.4f} XRP.")
        state = {"in_position": True, "entry_price": 0.0, "xrp_amount": xrp_balance}
    else:
        state = {"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0}
    save_state(state)
    return state

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    if not PUBLIC_KEY or not PRIVATE_KEY:
        log.error("Missing API keys!")
        return

    client = ChainEXClient(PUBLIC_KEY, PRIVATE_KEY)

    try:
        init_bal = client.get_balances()
        init_xrp = init_bal.get("XRP", {}).get("available", 0.0)
    except Exception as e:
        log.warning(f"Startup balance check failed: {e}")
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
                log.warning("No candles — check trade history endpoint.")
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
                    log.warning(f"ZAR too low (R{zar_bal:.2f})")
                else:
                    book      = client.get_orderbook(COIN, EXCHANGE)
                    asks      = book.get("ask", [])
                    buy_price = float(asks[0]["price"]) * 1.001 if asks else current_price * 1.002
                    xrp_qty   = spend / buy_price
                    log.info(f"BUY {xrp_qty:.4f} XRP @ R{buy_price:.4f} (R{spend:.2f})")
                    client.place_order(COIN, EXCHANGE, buy_price, xrp_qty, "buy")
                    state.update({"in_position": True, "entry_price": buy_price,
                                  "xrp_amount": xrp_qty})
                    save_state(state)

            elif signal == "SELL" and state["in_position"]:
                sell_qty = (min(state["xrp_amount"], xrp_bal)
                            if state["xrp_amount"] > 0 else xrp_bal) * 0.99
                if sell_qty < 1:
                    log.warning(f"Not enough XRP ({xrp_bal:.4f}). Resetting.")
                    state.update({"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
                else:
                    book       = client.get_orderbook(COIN, EXCHANGE)
                    bids       = book.get("bid", [])
                    sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998
                    pnl_str    = (f"R{(sell_price - state['entry_price']) * sell_qty:.2f}"
                                  if state["entry_price"] > 0 else "unknown entry")
                    log.info(f"SELL {sell_qty:.4f} XRP @ R{sell_price:.4f} (PnL: {pnl_str})")
                    client.place_order(COIN, EXCHANGE, sell_price, sell_qty, "sell")
                    state.update({"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
            else:
                log.info("HOLD.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
        except Exception as e:
            log.exception(f"Error: {e}")

        sleep_for = max(POLL_SECS - (time.time() - t0), 10)
        log.info(f"Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
