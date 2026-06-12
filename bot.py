"""
ChainEX XRP/ZAR EMA Crossover Bot  (v5 — CoinGecko price data)
Strategy: 9/21 EMA crossover + RSI filter
Price data: CoinGecko free API (no key needed)
Orders/Balances: ChainEX API
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

# ── Config ────────────────────────────────────────────────────────────────────
PUBLIC_KEY  = os.environ.get("CHAINEX_PUBLIC_KEY", "")
PRIVATE_KEY = os.environ.get("CHAINEX_PRIVATE_KEY", "")

COIN     = "XRP"
EXCHANGE = "ZAR"

EMA_FAST   = int(os.environ.get("EMA_FAST", 9))
EMA_SLOW   = int(os.environ.get("EMA_SLOW", 21))
RSI_PERIOD = int(os.environ.get("RSI_PERIOD", 14))
RSI_OB     = float(os.environ.get("RSI_OB", 70))
RSI_OS     = float(os.environ.get("RSI_OS", 30))

TRADE_ZAR  = float(os.environ.get("TRADE_ZAR", 0))
TRADE_PCT  = float(os.environ.get("TRADE_PCT", 0.95))

START_IN_POSITION = os.environ.get("START_IN_POSITION", "true").lower() == "true"
POLL_SECS  = int(os.environ.get("POLL_SECS", 300))   # 5 min candles

# CoinGecko: 'ripple' = XRP, vs_currency = 'zar', days of history
COINGECKO_COIN = "ripple"
COINGECKO_VS   = "zar"
COINGECKO_DAYS = 2    # last 2 days gives 5-min candles (free tier)

CHAINEX_BASE = "https://api.chainex.io"


# ── Price data from CoinGecko ─────────────────────────────────────────────────

def fetch_candles() -> list:
    """
    CoinGecko /coins/{id}/market_chart -- free tier, no key needed.
    Returns hourly price points for last 2 days.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/{COINGECKO_COIN}"
           f"/market_chart?vs_currency={COINGECKO_VS}&days={COINGECKO_DAYS}"
           f"&interval=hourly")
    try:
        r = requests.get(url, timeout=15,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        prices = r.json().get("prices", [])
        candles = [
            {"ts": p[0] // 1000, "open": p[1], "high": p[1],
             "low": p[1], "close": p[1]}
            for p in prices
        ]
        log.info(f"CoinGecko: {len(candles)} price points (XRP/ZAR)")
        return candles
    except Exception as e:
        log.error(f"CoinGecko fetch failed: {e}")
        return []


def fetch_current_price() -> float:
    """Simple spot price from CoinGecko."""
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={COINGECKO_COIN}&vs_currencies={COINGECKO_VS}")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()[COINGECKO_COIN][COINGECKO_VS])
    except Exception as e:
        log.error(f"CoinGecko price fetch failed: {e}")
        return 0.0


# ── ChainEX API Client (orders + balances only) ───────────────────────────────

class ChainEXClient:
    def __init__(self, pub, priv):
        self.pub  = pub
        self.priv = priv

    def _sign(self, payload: str) -> str:
        return hmac.new(
            self.priv.encode(),
            payload.encode(),
            hashlib.sha256
        ).hexdigest()

    def _request(self, endpoint: str, body: dict = None) -> dict:
        """
        Try multiple auth styles until one works.
        Logs exactly what came back so we can tune if needed.
        """
        body = body or {}
        nonce    = str(int(time.time() * 1000))
        body_str = json.dumps(body, separators=(',', ':')) if body else "{}"

        # Style A: JSON body + header auth (most modern exchanges)
        try:
            sig = self._sign(body_str)
            headers = {
                "Content-Type":  "application/json",
                "api-key":       self.pub,
                "api-nonce":     nonce,
                "api-signature": sig,
            }
            r = requests.post(f"{CHAINEX_BASE}{endpoint}",
                              headers=headers, data=body_str, timeout=10)
            log.debug(f"[A] POST {endpoint} → {r.status_code}: {r.text[:120]}")
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.debug(f"[A] failed: {e}")

        # Style B: form-encoded body + query-string signature
        try:
            params = {**body, "nonce": nonce, "api_key": self.pub}
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            params["signature"] = self._sign(qs)
            r = requests.post(f"{CHAINEX_BASE}{endpoint}",
                              data=params, timeout=10)
            log.debug(f"[B] POST {endpoint} → {r.status_code}: {r.text[:120]}")
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.debug(f"[B] failed: {e}")

        # Style C: GET with query-string signature
        try:
            params = {**body, "nonce": nonce, "api_key": self.pub}
            qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
            params["signature"] = self._sign(qs)
            r = requests.get(f"{CHAINEX_BASE}{endpoint}",
                             params=params, timeout=10)
            log.debug(f"[C] GET  {endpoint} → {r.status_code}: {r.text[:120]}")
            if r.status_code == 200:
                return r.json()
        except Exception as e:
            log.debug(f"[C] failed: {e}")

        log.warning(f"All auth styles failed for {endpoint}")
        return {}

    def get_balances(self) -> dict:
        for ep in ["/wallet/balances", "/account/balance",
                   "/user/balance", "/balances"]:
            data = self._request(ep)
            raw  = data.get("data", [])
            if not raw:
                continue
            if isinstance(raw, dict):
                raw = list(raw.values())
            balances = {}
            for item in raw:
                if not isinstance(item, dict):
                    continue
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
                log.info(f"Balances OK via {ep}: {list(balances.keys())}")
                return balances
        log.warning("Balance fetch failed — using 0 values.")
        return {}

    def get_orderbook(self) -> dict:
        for ep in [f"/market/orderbook/XRP_ZAR",
                   f"/market/orderbook/XRP/ZAR",
                   f"/orderbook/XRP_ZAR"]:
            try:
                r = requests.get(f"{CHAINEX_BASE}{ep}", timeout=10)
                if r.status_code == 200:
                    data = r.json().get("data", {})
                    if data:
                        log.info(f"Orderbook OK via {ep}")
                        return data
            except Exception:
                pass
        return {}

    def place_order(self, price: float, amount: float, side: str) -> dict:
        body = {
            "coin_code":     COIN,
            "exchange_code": EXCHANGE,
            "price":         str(round(price, 4)),
            "amount":        str(round(amount, 4)),
            "type":          side,   # "buy" or "sell"
        }
        for ep in ["/trading/addorder", "/order/add", "/trade/add"]:
            data = self._request(ep, body)
            if data:
                log.info(f"Order placed via {ep}: {data}")
                return data
        log.error("Order placement failed on all endpoints.")
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
    gains  = [max(prices[i] - prices[i-1], 0)      for i in range(1, period+1)]
    losses = [abs(min(prices[i] - prices[i-1], 0)) for i in range(1, period+1)]
    ag, al = sum(gains)/period, sum(losses)/period
    for i in range(period, len(prices)):
        if i > period:
            d  = prices[i] - prices[i-1]
            ag = (ag*(period-1) + max(d, 0))       / period
            al = (al*(period-1) + abs(min(d, 0)))  / period
        rs = ag/al if al else float('inf')
        result[i] = 100 - (100/(1+rs))
    return result

def get_signal(candles):
    if len(candles) < EMA_SLOW + 5:
        log.warning(f"Not enough candles ({len(candles)}) — need {EMA_SLOW+5}")
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
    rsi_str = f"{rsi_v[i]:.1f}" if rsi_v[i] is not None else "N/A"
    log.info(f"Price:{closes[-1]:.4f}  EMA{EMA_FAST}:{fast[i]:.4f}  "
             f"EMA{EMA_SLOW}:{slow[i]:.4f}  RSI:{rsi_str}")
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
        log.info(f"Bootstrapping in_position=True with {xrp_balance:.4f} XRP.")
        state = {"in_position": True, "entry_price": 0.0, "xrp_amount": xrp_balance}
    else:
        state = {"in_position": False, "entry_price": 0.0, "xrp_amount": 0.0}
    save_state(state)
    return state

def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f)


# ── Main Loop ─────────────────────────────────────────────────────────────────

def run():
    if not PUBLIC_KEY or not PRIVATE_KEY:
        log.error("Missing CHAINEX_PUBLIC_KEY or CHAINEX_PRIVATE_KEY!")
        return

    client = ChainEXClient(PUBLIC_KEY, PRIVATE_KEY)

    # Bootstrap: get live XRP balance for initial state
    try:
        init_bal = client.get_balances()
        init_xrp = init_bal.get("XRP", {}).get("available", 0.0)
    except Exception as e:
        log.warning(f"Startup balance check failed: {e}")
        init_xrp = 0.0

    state = load_state(xrp_balance=init_xrp)

    log.info("=" * 55)
    log.info(f"  ChainEX XRP/ZAR Bot  v5 | EMA {EMA_FAST}/{EMA_SLOW}")
    log.info(f"  Price source: CoinGecko (XRP/ZAR)")
    log.info(f"  RSI OB={RSI_OB} OS={RSI_OS} | Sizing: {int(TRADE_PCT*100)}%")
    log.info(f"  Poll: {POLL_SECS}s | In position: {state['in_position']}")
    log.info(f"  XRP tracked: {state['xrp_amount']:.4f}")
    log.info("=" * 55)

    while True:
        try:
            t0 = time.time()

            # ── 1. Get candles from CoinGecko ─────────────────────────────
            candles = fetch_candles()
            if not candles:
                log.warning("No candle data this cycle.")
                time.sleep(POLL_SECS)
                continue

            current_price = fetch_current_price() or candles[-1]["close"]
            log.info(f"XRP/ZAR spot: R{current_price:.4f}")

            # ── 2. Signal ─────────────────────────────────────────────────
            signal = get_signal(candles)
            log.info(f"Signal: {signal}")

            # ── 3. Balances ───────────────────────────────────────────────
            balances = client.get_balances()
            zar_bal  = balances.get("ZAR", {}).get("available", 0.0)
            xrp_bal  = balances.get("XRP", {}).get("available", 0.0)
            log.info(f"ZAR: R{zar_bal:.2f}  XRP: {xrp_bal:.4f}")

            # ── 4. Execute ────────────────────────────────────────────────
            if signal == "BUY" and not state["in_position"]:
                spend = TRADE_ZAR if TRADE_ZAR > 0 else zar_bal * TRADE_PCT
                spend = min(spend, zar_bal)
                if spend < 10:
                    log.warning(f"ZAR too low to buy (R{zar_bal:.2f})")
                else:
                    book      = client.get_orderbook()
                    asks      = book.get("ask", [])
                    buy_price = (float(asks[0]["price"]) * 1.001
                                 if asks else current_price * 1.002)
                    xrp_qty   = spend / buy_price
                    log.info(f"BUY {xrp_qty:.4f} XRP @ R{buy_price:.4f} (R{spend:.2f})")
                    client.place_order(buy_price, xrp_qty, "buy")
                    state.update({"in_position": True,
                                  "entry_price": buy_price,
                                  "xrp_amount":  xrp_qty})
                    save_state(state)

            elif signal == "SELL" and state["in_position"]:
                sell_qty = (min(state["xrp_amount"], xrp_bal)
                            if state["xrp_amount"] > 0 else xrp_bal) * 0.99
                if sell_qty < 1:
                    log.warning(f"Not enough XRP ({xrp_bal:.4f}). Resetting state.")
                    state.update({"in_position": False,
                                  "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
                else:
                    book       = client.get_orderbook()
                    bids       = book.get("bid", [])
                    sell_price = (float(bids[0]["price"]) * 0.999
                                  if bids else current_price * 0.998)
                    pnl = ((sell_price - state["entry_price"]) * sell_qty
                           if state["entry_price"] > 0 else None)
                    pnl_str = f"R{pnl:.2f}" if pnl is not None else "unknown entry"
                    log.info(f"SELL {sell_qty:.4f} XRP @ R{sell_price:.4f} (PnL: {pnl_str})")
                    client.place_order(sell_price, sell_qty, "sell")
                    state.update({"in_position": False,
                                  "entry_price": 0.0, "xrp_amount": 0.0})
                    save_state(state)
            else:
                log.info("HOLD — no action this cycle.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        sleep_for = max(POLL_SECS - (time.time() - t0), 10)
        log.info(f"Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
