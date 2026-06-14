"""
ChainEX XRP/ZAR MSG Strategy Bot
Strategy : MSG — EMA 21/55 + RSI + Volume confirmation (all 4 required)
Price data: CoinGecko free API (no key needed)
Orders    : ChainEX API
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

# ── MSG Indicator Settings ────────────────────────────────────────────────────
EMA_FAST   = 21      # MSG: was 9
EMA_SLOW   = 55      # MSG: was 21
RSI_PERIOD = 14
RSI_BULL   = 55.0    # MSG: RSI must be above 55 for buy (was OB=70)
RSI_BEAR   = 45.0    # MSG: RSI must be below 45 for sell (was OS=30)

# ── ATR Settings ──────────────────────────────────────────────────────────────
ATR_PERIOD        = 14
ATR_SL_MULTIPLIER = 1.5
ATR_TP_MULTIPLIER = 1.2

# ── Trade Sizing ──────────────────────────────────────────────────────────────
TRADE_ZAR  = float(os.environ.get("TRADE_ZAR", 0))
TRADE_PCT  = float(os.environ.get("TRADE_PCT", 0.95))

START_IN_POSITION = os.environ.get("START_IN_POSITION", "true").lower() == "true"
POLL_SECS  = int(os.environ.get("POLL_SECS", 300))   # 5 min candles

# ── CoinGecko Settings ────────────────────────────────────────────────────────
COINGECKO_COIN = "ripple"
COINGECKO_VS   = "zar"
COINGECKO_DAYS = 3    # 3 days gives enough hourly candles for EMA55

CHAINEX_BASE = "https://api.chainex.io"


# ── Price Data from CoinGecko ─────────────────────────────────────────────────

def fetch_candles() -> list:
    """
    CoinGecko /coins/{id}/market_chart — free tier, no key needed.
    Returns hourly price points — need at least 60+ for EMA55.
    """
    url = (f"https://api.coingecko.com/api/v3/coins/{COINGECKO_COIN}"
           f"/market_chart?vs_currency={COINGECKO_VS}&days={COINGECKO_DAYS}"
           f"&interval=hourly")
    try:
        r = requests.get(url, timeout=15,
                         headers={"Accept": "application/json"})
        r.raise_for_status()
        prices  = r.json().get("prices", [])
        volumes = r.json().get("total_volumes", [])

        # Zip price and volume together
        candles = []
        for i, p in enumerate(prices):
            vol = volumes[i][1] if i < len(volumes) else 0
            candles.append({
                "ts":     p[0] // 1000,
                "close":  p[1],
                "volume": vol
            })
        log.info(f"CoinGecko: {len(candles)} candles (XRP/ZAR)")
        return candles
    except Exception as e:
        log.error(f"CoinGecko fetch failed: {e}")
        return []


def fetch_current_price() -> float:
    url = (f"https://api.coingecko.com/api/v3/simple/price"
           f"?ids={COINGECKO_COIN}&vs_currencies={COINGECKO_VS}")
    try:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        return float(r.json()[COINGECKO_COIN][COINGECKO_VS])
    except Exception as e:
        log.error(f"CoinGecko price fetch failed: {e}")
        return 0.0


# ── ChainEX API Client ────────────────────────────────────────────────────────

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
        body     = body or {}
        nonce    = str(int(time.time() * 1000))
        body_str = json.dumps(body, separators=(',', ':')) if body else "{}"

        # Style A: JSON body + header auth
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
            "type":          side,
        }
        for ep in ["/trading/addorder", "/order/add", "/trade/add"]:
            data = self._request(ep, body)
            if data:
                log.info(f"Order placed via {ep}: {data}")
                return data
        log.error("Order placement failed on all endpoints.")
        return {}


# ── Indicators ────────────────────────────────────────────────────────────────

def calc_ema(prices, period):
    k      = 2 / (period + 1)
    result = [None] * len(prices)
    if len(prices) < period:
        return result
    result[period - 1] = sum(prices[:period]) / period
    for i in range(period, len(prices)):
        result[i] = prices[i] * k + result[i - 1] * (1 - k)
    return result

def calc_rsi(prices, period=14):
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

def calc_atr(candles, period=14):
    """ATR using close-to-close since CoinGecko only gives close prices."""
    closes = [c["close"] for c in candles]
    trs    = [abs(closes[i] - closes[i-1]) for i in range(1, len(closes))]
    if len(trs) < period:
        return 0.0
    atr = sum(trs[:period]) / period
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
    return atr


# ── MSG Signal Logic ──────────────────────────────────────────────────────────

def get_signal(candles):
    if len(candles) < EMA_SLOW + 5:
        log.warning(f"Not enough candles ({len(candles)}) — need {EMA_SLOW + 5}")
        return "HOLD", 0.0

    closes  = [c["close"]  for c in candles]
    volumes = [c["volume"] for c in candles]

    ema21   = calc_ema(closes, EMA_FAST)
    ema55   = calc_ema(closes, EMA_SLOW)
    rsi_val = calc_rsi(closes, RSI_PERIOD)

    i = len(closes) - 1
    # Walk back to find valid index
    while i > 0 and any(v is None for v in [ema21[i], ema55[i]]):
        i -= 1
    if i == 0:
        return "HOLD", 0.0

    curr_ema21 = ema21[i]
    curr_ema55 = ema55[i]
    curr_rsi   = rsi_val[i]
    curr_vol   = volumes[i]
    prev_vol   = volumes[i-1]
    curr_price = closes[i]

    # ── MSG Rule 1: Trend ─────────────────────────────────────────────────────
    bull_trend = curr_ema21 > curr_ema55
    bear_trend = curr_ema21 < curr_ema55

    # ── MSG Rule 2: Price vs EMA21 ────────────────────────────────────────────
    price_above_ema = curr_price > curr_ema21
    price_below_ema = curr_price < curr_ema21

    # ── MSG Rule 3: RSI ───────────────────────────────────────────────────────
    rsi_bull = curr_rsi is not None and curr_rsi > RSI_BULL
    rsi_bear = curr_rsi is not None and curr_rsi < RSI_BEAR

    # ── MSG Rule 4: Volume — current > previous bar ───────────────────────────
    vol_confirm = curr_vol > prev_vol

    rsi_str = f"{curr_rsi:.1f}" if curr_rsi is not None else "N/A"

    log.info(
        f"MSG | Price: R{curr_price:.4f} | "
        f"EMA21: {curr_ema21:.4f} | EMA55: {curr_ema55:.4f} | "
        f"RSI: {rsi_str} | Vol>Prev: {vol_confirm} | "
        f"BullTrend: {bull_trend} | BearTrend: {bear_trend}"
    )

    # ── All 4 conditions must be true ─────────────────────────────────────────
    atr = calc_atr(candles[-ATR_PERIOD*2:], ATR_PERIOD)

    if bull_trend and price_above_ema and rsi_bull and vol_confirm:
        log.info("✅ MSG BUY — all 4 conditions met")
        return "BUY", atr

    elif bear_trend and price_below_ema and rsi_bear and vol_confirm:
        log.info("🔴 MSG SELL — all 4 conditions met")
        return "SELL", atr

    else:
        log.info("⏳ HOLD — not all MSG conditions met")
        return "HOLD", atr


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
        state = {"in_position": True, "entry_price": 0.0,
                 "xrp_amount": xrp_balance, "stop_loss": 0.0, "take_profit": 0.0}
    else:
        state = {"in_position": False, "entry_price": 0.0,
                 "xrp_amount": 0.0, "stop_loss": 0.0, "take_profit": 0.0}
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

    try:
        init_bal = client.get_balances()
        init_xrp = init_bal.get("XRP", {}).get("available", 0.0)
    except Exception as e:
        log.warning(f"Startup balance check failed: {e}")
        init_xrp = 0.0

    state = load_state(xrp_balance=init_xrp)

    log.info("=" * 60)
    log.info(f"  ChainEX XRP/ZAR — MSG Strategy Bot")
    log.info(f"  EMA: {EMA_FAST}/{EMA_SLOW} | RSI Bull: >{RSI_BULL} | RSI Bear: <{RSI_BEAR}")
    log.info(f"  ATR SL: {ATR_SL_MULTIPLIER}x | ATR TP: {ATR_TP_MULTIPLIER}x")
    log.info(f"  Poll: {POLL_SECS}s | In position: {state['in_position']}")
    log.info(f"  XRP tracked: {state['xrp_amount']:.4f}")
    log.info("=" * 60)

    while True:
        try:
            t0 = time.time()

            # ── 1. Fetch candles ──────────────────────────────────────────
            candles = fetch_candles()
            if not candles:
                log.warning("No candle data this cycle.")
                time.sleep(POLL_SECS)
                continue

            current_price = fetch_current_price() or candles[-1]["close"]
            log.info(f"XRP/ZAR spot: R{current_price:.4f}")

            # ── 2. MSG Signal ─────────────────────────────────────────────
            signal, atr = get_signal(candles)
            log.info(f"Signal: {signal} | ATR: {atr:.4f}")

            # ── 3. Balances ───────────────────────────────────────────────
            balances = client.get_balances()
            zar_bal  = balances.get("ZAR", {}).get("available", 0.0)
            xrp_bal  = balances.get("XRP", {}).get("available", 0.0)
            log.info(f"ZAR: R{zar_bal:.2f}  XRP: {xrp_bal:.4f}")

            # ── 4. SL/TP Check while in position ─────────────────────────
            if state["in_position"] and state["stop_loss"] and state["take_profit"]:
                if current_price <= state["stop_loss"]:
                    log.info(f"🛑 Stop loss hit | Price: R{current_price:.4f} | SL: R{state['stop_loss']:.4f}")
                    sell_qty = min(state["xrp_amount"], xrp_bal) * 0.99
                    if sell_qty >= 1:
                        book       = client.get_orderbook()
                        bids       = book.get("bid", [])
                        sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998
                        client.place_order(sell_price, sell_qty, "sell")
                    state.update({"in_position": False, "entry_price": 0.0,
                                  "xrp_amount": 0.0, "stop_loss": 0.0, "take_profit": 0.0})
                    save_state(state)
                    time.sleep(POLL_SECS)
                    continue

                elif current_price >= state["take_profit"]:
                    log.info(f"🎯 Take profit hit | Price: R{current_price:.4f} | TP: R{state['take_profit']:.4f}")
                    sell_qty = min(state["xrp_amount"], xrp_bal) * 0.99
                    if sell_qty >= 1:
                        book       = client.get_orderbook()
                        bids       = book.get("bid", [])
                        sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998
                        client.place_order(sell_price, sell_qty, "sell")
                    state.update({"in_position": False, "entry_price": 0.0,
                                  "xrp_amount": 0.0, "stop_loss": 0.0, "take_profit": 0.0})
                    save_state(state)
                    time.sleep(POLL_SECS)
                    continue

            # ── 5. Execute Signal ─────────────────────────────────────────
            if signal == "BUY" and not state["in_position"]:
                spend = TRADE_ZAR if TRADE_ZAR > 0 else zar_bal * TRADE_PCT
                spend = min(spend, zar_bal)
                if spend < 10:
                    log.warning(f"ZAR too low to buy (R{zar_bal:.2f})")
                else:
                    book      = client.get_orderbook()
                    asks      = book.get("ask", [])
                    buy_price = float(asks[0]["price"]) * 1.001 if asks else current_price * 1.002
                    xrp_qty   = spend / buy_price
                    sl        = buy_price - (ATR_SL_MULTIPLIER * atr)
                    tp        = buy_price + (ATR_TP_MULTIPLIER * atr)
                    log.info(
                        f"✅ BUY {xrp_qty:.4f} XRP @ R{buy_price:.4f} "
                        f"(R{spend:.2f}) | SL: R{sl:.4f} | TP: R{tp:.4f}"
                    )
                    client.place_order(buy_price, xrp_qty, "buy")
                    state.update({
                        "in_position": True,
                        "entry_price": buy_price,
                        "xrp_amount":  xrp_qty,
                        "stop_loss":   sl,
                        "take_profit": tp,
                    })
                    save_state(state)

            elif signal == "SELL" and state["in_position"]:
                sell_qty = (min(state["xrp_amount"], xrp_bal)
                            if state["xrp_amount"] > 0 else xrp_bal) * 0.99
                if sell_qty < 1:
                    log.warning(f"Not enough XRP ({xrp_bal:.4f}). Resetting state.")
                    state.update({"in_position": False, "entry_price": 0.0,
                                  "xrp_amount": 0.0, "stop_loss": 0.0, "take_profit": 0.0})
                    save_state(state)
                else:
                    book       = client.get_orderbook()
                    bids       = book.get("bid", [])
                    sell_price = float(bids[0]["price"]) * 0.999 if bids else current_price * 0.998
                    pnl        = (sell_price - state["entry_price"]) * sell_qty if state["entry_price"] > 0 else None
                    pnl_str    = f"R{pnl:.2f}" if pnl is not None else "unknown entry"
                    log.info(f"🔴 SELL {sell_qty:.4f} XRP @ R{sell_price:.4f} | PnL: {pnl_str}")
                    client.place_order(sell_price, sell_qty, "sell")
                    state.update({"in_position": False, "entry_price": 0.0,
                                  "xrp_amount": 0.0, "stop_loss": 0.0, "take_profit": 0.0})
                    save_state(state)

            else:
                log.info("⏳ HOLD — no MSG signal this cycle.")

        except requests.exceptions.RequestException as e:
            log.error(f"Network error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        sleep_for = max(POLL_SECS - (time.time() - t0), 10)
        log.info(f"Sleeping {sleep_for:.0f}s...\n")
        time.sleep(sleep_for)


if __name__ == "__main__":
    run()
