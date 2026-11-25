#!/usr/bin/env python3
"""
luxalgo_upstox_multi_tf.py  (3m + 5m + 15m in one process)
============================================
Production Engine for NIFTY Options:
- Strategy 1: Trendline Breakouts (LuxAlgo "B" labels)
- Strategy 2: Support & Resistance Bounces (Mean Reversion)

Each timeframe holds its own independent position with its own risk settings.

Token: prefer env UPSTOX_TOKEN (same secret name as other LuxAlgo repos),
then UPSTOX_ACCESS_TOKEN, then --token. Never commit real tokens.
"""
from __future__ import annotations
import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo
import requests

IST = ZoneInfo("Asia/Kolkata")
MARKET_OPEN = dtime(9, 15)
MARKET_CLOSE = dtime(15, 30)
FORCE_EXIT_TIME = dtime(15, 15)
ENTRY_CUTOFF = dtime(14, 30)

BASE_URL = "https://api.upstox.com/v2"
NIFTY_SPOT_KEY = "NSE_INDEX|Nifty 50"
STATE_FILE = Path("bot_state_multi.json")

STALE_GAP_SEC = 30  # a bar with a tick gap larger than this is considered unreliable
STALE_SAME_PX = 15  # this many identical consecutive spot polls (~45 s) => frozen quote

# Token resolution: UPSTOX_TOKEN (preferred) -> UPSTOX_ACCESS_TOKEN -> empty
UPSTOX_ACCESS_TOKEN = (
    os.environ.get("UPSTOX_TOKEN")
    or os.environ.get("UPSTOX_ACCESS_TOKEN")
    or ""
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("luxalgo_live_production_multi.log", encoding="utf-8")
    ]
)
logger = logging.getLogger("LuxMulti")


# ==========================================================================
# 1. LUXALGO INDICATOR ENGINE (BREAKOUTS + BOUNCES)
# ==========================================================================
def evaluate_trendlines(candles: list[dict], length: int = 14, mult: float = 1.0) -> dict:
    """Signals are evaluated on the LAST candle of `candles`, which must be a CLOSED bar."""
    n = len(candles)
    empty_result = {
        "upper": None, "lower": None,
        "up_break": False, "dn_break": False,
        "support_bounce": False, "resistance_reject": False,
        "new_upper": False, "new_lower": False,
        "upper_origin": None, "lower_origin": None
    }
    if n < (length * 2 + 1):
        return empty_result

    # 1. Wilder RMA ATR Calculation
    tr = [0.0] * n
    for i in range(n):
        c = candles[i]
        tr[i] = c["high"] - c["low"] if i == 0 else max(
            c["high"] - c["low"],
            abs(c["high"] - candles[i - 1]["close"]),
            abs(c["low"] - candles[i - 1]["close"])
        )
    atr = [None] * n
    if n >= length:
        a = sum(tr[:length]) / length
        atr[length - 1] = a
        for i in range(length, n):
            a = (a * (length - 1) + tr[i]) / length
            atr[i] = a

    upper = lower = None
    slope_ph = slope_pl = 0.0
    upos = dnos = 0
    up_break = dn_break = False
    support_bounce = resistance_reject = False
    new_upper = new_lower = False
    upper_origin = lower_origin = None
    upper_now = lower_now = None

    for i in range(n):
        p = i - length
        # Pivot High (Pine ta.pivothigh: left >=, right >)
        ph = candles[p]["high"] if (p >= length and
            all(candles[k]["high"] <= candles[p]["high"] for k in range(p - length, p)) and
            all(candles[k]["high"] < candles[p]["high"] for k in range(p + 1, p + length + 1))) else None

        # Pivot Low
        pl = candles[p]["low"] if (p >= length and
            all(candles[k]["low"] >= candles[p]["low"] for k in range(p - length, p)) and
            all(candles[k]["low"] > candles[p]["low"] for k in range(p + 1, p + length + 1))) else None

        s = (atr[i] / length * mult) if atr[i] is not None else None
        pivot_hi = ph is not None and s is not None
        pivot_lo = pl is not None and s is not None

        # Upper trendline: upper := ph ? ph : upper - slope_ph
        if pivot_hi:
            slope_ph, upper = s, ph
            if i == n - 1:
                new_upper = True
                upper_origin = {"time": candles[p]["time"], "price": ph, "slope": slope_ph}
        elif upper is not None:
            upper -= slope_ph

        # Lower trendline: lower := pl ? pl : lower + slope_pl
        if pivot_lo:
            slope_pl, lower = s, pl
            if i == n - 1:
                new_lower = True
                lower_origin = {"time": candles[p]["time"], "price": pl, "slope": slope_pl}
        elif lower is not None:
            lower += slope_pl

        upper_now = upper - slope_ph * length if upper is not None else None
        lower_now = lower + slope_pl * length if lower is not None else None

        c_open = candles[i]["open"]
        c_high = candles[i]["high"]
        c_low = candles[i]["low"]
        c_close = candles[i]["close"]

        # LuxAlgo state machine (exact Pine semantics):
        #   upos := ph ? 0 : close > line ? 1 : upos      'B' = upos rose 0 -> 1
        # On the bar where a new pivot is confirmed the state is forced to 0, so a 'B'
        # can never print on that bar (it can print on the next one).
        up_prev, dn_prev = upos, dnos
        if pivot_hi:
            upos = 0
        elif upper_now is not None and c_close > upper_now:
            upos = 1
        if pivot_lo:
            dnos = 0
        elif lower_now is not None and c_close < lower_now:
            dnos = 1

        # Only report signals for the latest closed candle
        if i == n - 1:
            up_break = upos > up_prev
            dn_break = dnos > dn_prev
            current_atr = atr[i] if atr[i] is not None else 15.0
            tol = max(4.0, current_atr * 0.20)  # Dynamic test tolerance

            # B. Support Bounce (CE Entry) - custom strategy, not a LuxAlgo label
            if lower_now is not None and dnos == 0:
                tested_support = c_low <= (lower_now + tol)
                closed_green_above = (c_close > lower_now) and (c_close > c_open)
                if tested_support and closed_green_above:
                    support_bounce = True

            # C. Resistance Rejection (PE Entry) - custom strategy, not a LuxAlgo label
            if upper_now is not None and upos == 0:
                tested_resistance = c_high >= (upper_now - tol)
                closed_red_below = (c_close < upper_now) and (c_close < c_open)
                if tested_resistance and closed_red_below:
                    resistance_reject = True

    return {
        "upper": upper_now,
        "lower": lower_now,
        "up_break": up_break,
        "dn_break": dn_break,
        "support_bounce": support_bounce,
        "resistance_reject": resistance_reject,
        "new_upper": new_upper,
        "new_lower": new_lower,
        "upper_origin": upper_origin,
        "lower_origin": lower_origin,
    }


# ==========================================================================
# 2. UPSTOX API ADAPTER
# ==========================================================================
class UpstoxClient:
    def __init__(self, token: str, mode: str = "paper", fill_timeout: int = 8, paper_slippage: float = 1.0):
        self.token = token
        self.mode = mode
        self.fill_timeout = fill_timeout
        self.paper_slippage = paper_slippage
        self.headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }
        self._spot_fail_count = 0

    def _fetch_candles(self, url: str, label: str) -> dict:
        out: dict[datetime, dict] = {}
        try:
            r = requests.get(url, headers=self.headers, timeout=10)
            if r.status_code == 200:
                for c in r.json().get("data", {}).get("candles", []):
                    dt = datetime.fromisoformat(c[0])
                    if dt.tzinfo:
                        dt = dt.astimezone(IST).replace(tzinfo=None)
                    out[dt] = {
                        "time": dt, "open": float(c[1]), "high": float(c[2]),
                        "low": float(c[3]), "close": float(c[4])
                    }
            else:
                logger.error(f"Warm-up fetch ({label}) HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"Warm-up fetch ({label}) failed: {e}")
        return out

    def fetch_intraday_candles(self) -> dict:
        """Official 1-minute candles for today (same data TradingView/NSE show)."""
        key = quote(NIFTY_SPOT_KEY, safe="")
        return self._fetch_candles(f"{BASE_URL}/historical-candle/intraday/{key}/1minute", "intraday-sync")

    def fetch_warmup_candles(self, days_back: int = 3) -> list[dict]:
        """Historical (previous days) + intraday (today) 1-minute candles, merged and sorted."""
        key = quote(NIFTY_SPOT_KEY, safe="")
        now = datetime.now(IST)
        to_date = now.strftime("%Y-%m-%d")
        from_date = (now - timedelta(days=days_back)).strftime("%Y-%m-%d")

        hist = self._fetch_candles(f"{BASE_URL}/historical-candle/{key}/1minute/{to_date}/{from_date}", "historical")
        intra = self._fetch_candles(f"{BASE_URL}/historical-candle/intraday/{key}/1minute", "intraday")

        merged = dict(hist)
        merged.update(intra)
        candles = [merged[k] for k in sorted(merged)]

        if candles:
            today_count = sum(1 for c in candles if c["time"].date() == now.date())
            logger.info(
                f"Loaded {len(candles)} 1-minute bars for pre-warming "
                f"(historical {len(hist)}, intraday {len(intra)}, today {today_count}). "
                f"Last bar: {candles[-1]['time'].strftime('%Y-%m-%d %H:%M')}"
            )
        return candles

    def _spot_ok(self):
        if self._spot_fail_count > 0:
            logger.info(f"✅ Connection restored after {self._spot_fail_count} failed spot polls.")
            self._spot_fail_count = 0

    def _spot_failed(self, msg: str):
        self._spot_fail_count += 1
        if self._spot_fail_count == 1 or self._spot_fail_count % 10 == 0:
            logger.warning(f"{msg} (failed polls so far: {self._spot_fail_count})")

    def get_spot_ltp(self) -> float | None:
        url = f"{BASE_URL}/market-quote/ltp"
        try:
            r = requests.get(url, headers=self.headers, params={"instrument_key": NIFTY_SPOT_KEY}, timeout=5)
            if r.status_code == 200:
                data = r.json().get("data", {})
                for v in data.values():
                    self._spot_ok()
                    return float(v["last_price"])
                self._spot_failed("Spot LTP returned empty data")
            elif r.status_code == 401:
                self._spot_failed("Spot LTP HTTP 401: access token invalid or expired")
            else:
                self._spot_failed(f"Spot LTP HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            self._spot_failed(f"Network error in get_spot_ltp: {type(e).__name__}: {str(e)[:150]}")
        return None

    def get_option_ltp(self, instrument_key: str) -> float | None:
        url = f"{BASE_URL}/market-quote/ltp"
        try:
            r = requests.get(url, headers=self.headers, params={"instrument_key": instrument_key}, timeout=5)
            if r.status_code == 200:
                data = r.json().get("data", {})
                for v in data.values():
                    return float(v["last_price"])
            else:
                logger.warning(f"Option LTP HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Network error in get_option_ltp ({instrument_key}): {type(e).__name__}: {str(e)[:150]}")
        return None

    def get_atm_contract(self, spot: float, opt_type: str) -> tuple[str, str] | None:
        strike = int(round(spot / 50.0) * 50)
        today = datetime.now(IST).strftime("%Y-%m-%d")
        try:
            r = requests.get(
                f"{BASE_URL}/option/contract",
                headers=self.headers,
                params={"instrument_key": NIFTY_SPOT_KEY},
                timeout=8,
            )
            if r.status_code == 200:
                contracts = []
                for c in r.json().get("data", []):
                    if int(round(float(c.get("strike_price", 0)))) != strike:
                        continue
                    if c.get("instrument_type") != opt_type:
                        continue
                    exp = c.get("expiry")
                    if isinstance(exp, str) and exp[:10] < today:
                        continue
                    contracts.append(c)
                if contracts:
                    contracts.sort(key=lambda x: str(x.get("expiry")))
                    return contracts[0]["instrument_key"], contracts[0]["trading_symbol"]
            else:
                logger.error(f"Option lookup failed HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.error(f"Option contract lookup error: {e}")
        return None

    def _order_details(self, order_id: str) -> dict | None:
        try:
            r = requests.get(f"{BASE_URL}/order/details", headers=self.headers,
                             params={"order_id": order_id}, timeout=5)
            if r.status_code == 200:
                return r.json().get("data") or None
            logger.warning(f"Order details HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Order details error: {type(e).__name__}: {str(e)[:150]}")
        return None

    def _cancel_order(self, order_id: str):
        try:
            r = requests.delete(f"{BASE_URL}/order/cancel", headers=self.headers,
                                params={"order_id": order_id}, timeout=5)
            logger.info(f"Cancel order {order_id} -> HTTP {r.status_code}: {r.text[:200]}")
        except Exception as e:
            logger.warning(f"Cancel order error: {type(e).__name__}: {str(e)[:150]}")

    def _result_from_details(self, d: dict, qty: int, order_id: str) -> dict:
        filled = int(float(d.get("filled_quantity") or 0))
        avg = float(d.get("average_price") or 0.0)
        if filled >= qty:
            status = "SUCCESS"
        elif filled > 0:
            status = "PARTIAL"
        else:
            status = "FAILED"
        return {"status": status, "fill_price": avg, "filled_qty": min(filled, qty), "order_id": order_id}

    def _await_fill(self, order_id: str, qty: int) -> dict:
        deadline = time.time() + self.fill_timeout
        last = None
        terminal = False
        while time.time() < deadline:
            d = self._order_details(order_id)
            if d:
                last = d
                st = str(d.get("status", "")).lower()
                if st == "complete":
                    terminal = True
                    break
                if st in ("rejected", "cancelled"):
                    terminal = True
                    logger.error(f"❌ Order {order_id} {st}: {d.get('status_message')}")
                    break
            time.sleep(1.0)

        if last is None:
            logger.critical(f"Could not read status of order {order_id} at all.")
            return {"status": "UNKNOWN", "fill_price": 0.0, "filled_qty": 0, "order_id": order_id}

        if not terminal:
            logger.warning(f"Order {order_id} not filled within {self.fill_timeout}s "
                           f"(status: {last.get('status')}). Cancelling.")
            self._cancel_order(order_id)
            time.sleep(1.0)
            d = self._order_details(order_id)
            if d:
                last = d

        return self._result_from_details(last, qty, order_id)

    def place_order(self, instrument_key: str, side: str, qty: int, slippage_pct: float = 3.0, tag: str = "luxalgo") -> dict:
        if self.mode == "paper":
            ltp = self.get_option_ltp(instrument_key)
            if not ltp or ltp <= 0:
                logger.error(f"[PAPER] Cannot fill {side}: no option LTP for {instrument_key}")
                return {"status": "FAILED", "fill_price": 0.0, "filled_qty": 0, "order_id": None}
            # Adverse paper slippage so paper P&L is not better than a real fill
            adj = (1 + self.paper_slippage / 100.0) if side == "BUY" else (1 - self.paper_slippage / 100.0)
            fill = max(0.05, round(round(ltp * adj / 0.05) * 0.05, 2))
            logger.info(f"[PAPER] {side} {qty} qty of {instrument_key} @ ~₹{fill:.2f} (LTP {ltp:.2f})")
            return {"status": "SUCCESS", "fill_price": fill, "filled_qty": qty, "order_id": "PAPER_ORDER"}

        ltp = self.get_option_ltp(instrument_key)
        if not ltp or ltp <= 0:
            logger.error(f"Cannot place order: Could not fetch LTP for {instrument_key}")
            return {"status": "FAILED", "fill_price": 0.0, "filled_qty": 0, "order_id": None}

        factor = (1 + slippage_pct / 100.0) if side == "BUY" else (1 - slippage_pct / 100.0)
        raw_px = ltp * factor
        limit_price = max(0.05, round(round(raw_px / 0.05) * 0.05, 2))

        payload = {
            "quantity": qty,
            "product": "D",
            "validity": "DAY",
            "price": limit_price,
            "tag": tag,
            "instrument_token": instrument_key,
            "order_type": "LIMIT",
            "transaction_type": side,
            "disclosed_quantity": 0,
            "trigger_price": 0.0,
            "is_amo": False
        }

        try:
            r = requests.post(f"{BASE_URL}/order/place", headers=self.headers, json=payload, timeout=8)
            res = r.json()
            logger.info(f"Upstox Order API Response [{r.status_code}]: {res}")

            if r.status_code == 200 and res.get("status") == "success":
                order_id = res.get("data", {}).get("order_id")
                if not order_id:
                    logger.critical("Order accepted but no order_id returned.")
                    return {"status": "UNKNOWN", "fill_price": 0.0, "filled_qty": 0, "order_id": None}
                out = self._await_fill(order_id, qty)
                if out["status"] in ("SUCCESS", "PARTIAL") and out["fill_price"] <= 0:
                    out["fill_price"] = self.get_option_ltp(instrument_key) or limit_price
                return out

            errors = res.get("errors", [])
            err_msg = errors[0].get("message") if errors else res.get("message", "Unknown error")
            logger.error(f"❌ Order Rejected by Upstox: {err_msg}")
        except requests.exceptions.ReadTimeout:
            logger.critical("Order request timed out after sending: order state UNKNOWN.")
            return {"status": "UNKNOWN", "fill_price": 0.0, "filled_qty": 0, "order_id": None}
        except Exception as e:
            logger.error(f"❌ Live order exception: {e}")

        return {"status": "FAILED", "fill_price": 0.0, "filled_qty": 0, "order_id": None}


# ==========================================================================
# 3. CONTINUOUS RESAMPLER
# ==========================================================================
class Resampler:
    """All bar times are tz-NAIVE IST wall-clock (same as the 1-minute warm-up data)."""

    def __init__(self, interval_sec: int, raw_candles: list[dict]):
        self.interval_sec = interval_sec
        self.last_tick: datetime | None = None
        self.bar_max_gap = 0.0
        self.bar_bad_feed = False
        self.candles: list[dict] = self._group(raw_candles)

    def _group(self, raw: list[dict]) -> list[dict]:
        step = self.interval_sec
        grouped: dict = {}
        for c in raw:
            dt = c["time"]
            anchor = dt.replace(hour=9, minute=15, second=0, microsecond=0)
            if dt < anchor:
                continue
            slot = int((dt - anchor).total_seconds() // step)
            grouped.setdefault((dt.date(), slot), (anchor + timedelta(seconds=slot * step), []))[1].append(c)

        out = []
        for _, (slot_start, grp) in sorted(grouped.items(), key=lambda x: x[0]):
            grp.sort(key=lambda x: x["time"])
            out.append({
                "time": slot_start,
                "open": grp[0]["open"],
                "high": max(x["high"] for x in grp),
                "low": min(x["low"] for x in grp),
                "close": grp[-1]["close"],
            })
        return out

    @staticmethod
    def _new_bar(slot_start: datetime, price: float) -> dict:
        return {"time": slot_start, "open": price, "high": price, "low": price, "close": price}

    def replace_closed(self, raw: list[dict], completed: dict, official_last: bool):
        """Rebuild every CLOSED bar from official 1-minute candles; keep the forming bar.
        If the official candle for the just-closed bar is not available yet, keep the polled one."""
        if len(self.candles) < 2 and not self.candles:
            return
        forming = self.candles[-1]
        closed = [b for b in self._group(raw) if b["time"] < forming["time"]]
        if not official_last:
            closed = [b for b in closed if b["time"] != completed["time"]]
            closed.append(completed)
            closed.sort(key=lambda b: b["time"])
        self.candles = closed + [forming]

    def on_tick(self, dt: datetime, price: float | None, feed_ok: bool = True) -> dict | None:
        dt = dt.replace(tzinfo=None)  # FIX: warm-up bars are naive; aware != naive made every seeded bar look "closed"
        anchor = dt.replace(hour=9, minute=15, second=0, microsecond=0)
        slot = int((dt - anchor).total_seconds() // self.interval_sec)
        slot_start = anchor + timedelta(seconds=slot * self.interval_sec)

        if price is None:  # frozen quote: only roll bars over, never invent a price move
            if not self.candles:
                return None
            price = self.candles[-1]["close"]

        gap = (dt - self.last_tick).total_seconds() if self.last_tick else 0.0
        self.last_tick = dt
        if not feed_ok:
            self.bar_bad_feed = True

        if not self.candles:
            self.candles.append(self._new_bar(slot_start, price))
            self.bar_max_gap = 0.0
            self.bar_bad_feed = not feed_ok
            return None

        last = self.candles[-1]
        if slot_start != last["time"]:
            completed = dict(last)
            completed["stale"] = max(self.bar_max_gap, gap) > STALE_GAP_SEC or self.bar_bad_feed
            self.candles.append(self._new_bar(slot_start, price))
            self.bar_max_gap = 0.0
            self.bar_bad_feed = not feed_ok
            return completed

        self.bar_max_gap = max(self.bar_max_gap, gap)
        if feed_ok:
            last["high"] = max(last["high"], price)
            last["low"] = min(last["low"], price)
            last["close"] = price
        return None


# ==========================================================================
# 4. MULTI-TIMEFRAME ENGINE (3m + 5m + 15m in ONE process)
# ==========================================================================
DEFAULT_CFG = {
    3:  {"sl": 15.0, "be_at": 6.0,  "trail_at": 8.0,  "giveback": 20.0},
    5:  {"sl": 20.0, "be_at": 8.0,  "trail_at": 11.0, "giveback": 22.0},
    15: {"sl": 25.0, "be_at": 10.0, "trail_at": 14.0, "giveback": 25.0},
}


def parse_map(text: str | None, name: str) -> dict[int, float]:
    """'3=15,5=20' -> {3: 15.0, 5: 20.0}"""
    out: dict[int, float] = {}
    if not text:
        return out
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            k, v = part.split("=")
            out[int(k)] = float(v)
        except ValueError:
            raise SystemExit(f"Bad value for --{name}: '{part}' (use e.g. 3=15,5=20)")
    return out


class MultiEngine:
    def __init__(self, args):
        self.args = args
        self.tfs: list[int] = args.intervals
        self.cfg = {tf: dict(DEFAULT_CFG.get(tf, DEFAULT_CFG[5])) for tf in self.tfs}
        for flag, key in (("sl", "sl"), ("be_at", "be_at"), ("trail_at", "trail_at"), ("giveback", "giveback")):
            for tf, val in parse_map(getattr(args, flag), flag.replace("_", "-")).items():
                if tf in self.cfg:
                    self.cfg[tf][key] = val

        self.client = UpstoxClient(token=args.token, mode=args.mode, fill_timeout=args.fill_timeout,
                                   paper_slippage=args.paper_slippage)
        raw_warmup = self._load_warmup()
        self.base_raw = {c["time"]: c for c in raw_warmup}
        self._last_spot: float | None = None
        self._same_px = 0
        self.quote_stale = False
        self.resamplers = {tf: Resampler(tf * 60, raw_warmup) for tf in self.tfs}
        self.state = self._load_state()
        self.next_exit_try = {tf: 0.0 for tf in self.tfs}

    def _load_warmup(self) -> list[dict]:
        for attempt in range(1, 4):
            raw = self.client.fetch_warmup_candles(days_back=8)
            if raw:
                return raw
            logger.warning(f"Warm-up attempt {attempt}/3 returned no data.")
            time.sleep(3)
        logger.error("Warm-up failed: indicators need live bars before they can signal.")
        return []

    def _fresh_state(self, today: str) -> dict:
        return {
            "date": today,
            "positions": {str(tf): None for tf in self.tfs},
            "trades_today": {str(tf): 0 for tf in self.tfs},
        }

    def _load_state(self) -> dict:
        today = str(datetime.now(IST).date())
        state = None
        if STATE_FILE.exists():
            try:
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    state = json.load(f)
            except Exception:
                state = None
        if not isinstance(state, dict):
            return self._fresh_state(today)

        state.setdefault("positions", {})
        state.setdefault("trades_today", {})
        for tf in self.tfs:
            state["positions"].setdefault(str(tf), None)
            state["trades_today"].setdefault(str(tf), 0)
        if state.get("date") != today:
            state["trades_today"] = {str(tf): 0 for tf in self.tfs}
            state["date"] = today
        for tf in self.tfs:
            p = state["positions"].get(str(tf))
            if p:
                logger.warning(f"⚠️ [{tf}m] Resuming with OPEN position from state file: {p.get('symbol')} "
                               f"qty {p.get('qty')} entry ₹{p.get('entry')}. Verify it exists at your broker.")
        return state

    def _save_state(self):
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(self.state, f, indent=2)

    def _roll_date(self, now: datetime):
        today = str(now.date())
        if self.state.get("date") != today:
            logger.info(f"New trading day {today}: resetting trades_today.")
            self.state["date"] = today
            self.state["trades_today"] = {str(tf): 0 for tf in self.tfs}
            self._save_state()

    def _halt(self, msg: str):
        logger.critical(f"🛑 HALTING BOT: {msg}")
        logger.critical("Check your Upstox app (orders + positions) before restarting. "
                        "State file keeps every recorded position.")
        self._save_state()
        sys.exit(2)

    def _pos(self, tf: int) -> dict | None:
        return self.state["positions"].get(str(tf))

    def _open_tfs(self) -> list[int]:
        return [tf for tf in self.tfs if self._pos(tf)]

    def run(self):
        logger.info(f"=== MULTI-TIMEFRAME BOT STARTED [{self.args.mode.upper()}] ===")
        logger.info("Strategies: BREAKOUTS + SUPPORT/RESISTANCE BOUNCES")
        for tf in self.tfs:
            c = self.cfg[tf]
            logger.info(f"[{tf}m] SL {c['sl']}% | BE +{c['be_at']}% | Trail +{c['trail_at']}% "
                        f"(giveback {c['giveback']}%) | Qty {self.args.qty} | Lookback {self.args.length}")
        logger.info("Each timeframe holds its own position independently.")

        last_heartbeat = time.time()

        while True:
            try:
                now = datetime.now(IST)
                t_now = now.time()

                if not (MARKET_OPEN <= t_now <= MARKET_CLOSE):
                    time.sleep(5)
                    continue

                self._roll_date(now)

                if t_now >= FORCE_EXIT_TIME and self._open_tfs():
                    for tf in self._open_tfs():
                        self.exit_trade(tf, "Market Close Force Exit")
                    time.sleep(2.0)
                    continue

                spot = self.client.get_spot_ltp()
                if spot is None:
                    if self._open_tfs():
                        self.manage_positions()
                    time.sleep(2)
                    continue

                # Frozen-quote detection (the index LTP returned the same value for 13 minutes after 15:15)
                if self._last_spot is not None and spot == self._last_spot:
                    self._same_px += 1
                else:
                    self._same_px = 0
                self._last_spot = spot
                stale_now = self._same_px >= STALE_SAME_PX
                if stale_now and not self.quote_stale:
                    logger.warning(f"⚠️ Spot quote frozen at ₹{spot:.2f} for {self._same_px} polls: "
                                   f"ignoring it for bar building and blocking new entries.")
                elif self.quote_stale and not stale_now:
                    logger.info("✅ Spot quote moving again.")
                self.quote_stale = stale_now

                if time.time() - last_heartbeat >= 30:
                    parts = []
                    for tf in self.tfs:
                        p = self._pos(tf)
                        parts.append(f"{tf}m: {p['symbol']} x{p['qty']} SL ₹{p['current_sl']:.2f}" if p
                                     else f"{tf}m: flat")
                    logger.info(f"💓 [HEARTBEAT] Spot: ₹{spot:.2f}{' (STALE)' if self.quote_stale else ''} | "
                                f"{now.strftime('%H:%M:%S')} | " + " | ".join(parts))
                    last_heartbeat = time.time()

                if self._open_tfs():
                    self.manage_positions()

                completions: dict[int, dict] = {}
                for tf in self.tfs:
                    completed = self.resamplers[tf].on_tick(
                        now, None if self.quote_stale else spot, feed_ok=not self.quote_stale)
                    if completed and completed["time"].date() == now.date():
                        completions[tf] = completed

                if completions:
                    self.sync_official(completions)
                    for tf, polled in completions.items():
                        rs = self.resamplers[tf]
                        bar = rs.candles[-2] if len(rs.candles) >= 2 else polled
                        if bar["time"] != polled["time"]:
                            bar = polled
                        logger.info(
                            f"[{tf}m] Closed bar [{bar['time'].strftime('%H:%M')}]: "
                            f"O: {bar['open']:.2f} H: {bar['high']:.2f} "
                            f"L: {bar['low']:.2f} C: {bar['close']:.2f}"
                            f"{' (polled)' if bar is polled else ''}"
                        )
                        self.process_signal(tf, bar, now)

                time.sleep(3.0)

            except KeyboardInterrupt:
                logger.info("Bot stopped by user.")
                break
            except SystemExit:
                raise
            except Exception as e:
                logger.error(f"Runtime loop error: {e}")
                time.sleep(2)

    def sync_official(self, completions: dict[int, dict]):
        """Replace polled bars with official 1-minute-derived bars (identical to TradingView).
        Waits up to --sync-wait seconds for the last minute of the closed bar to be published;
        if it is not, the polled version of that single bar is kept (and corrected next time)."""
        needed = {
            tf: c["time"] + timedelta(seconds=self.resamplers[tf].interval_sec) - timedelta(minutes=1)
            for tf, c in completions.items()
        }
        deadline = time.time() + self.args.sync_wait
        official: dict = {}
        while True:
            intra = self.client.fetch_intraday_candles()
            if intra:
                official = intra
            if official and all(n in official for n in needed.values()):
                break
            if time.time() >= deadline:
                break
            if self._open_tfs():
                self.manage_positions()
            time.sleep(1.0)

        if not official:
            logger.warning("Official candle sync failed; using polled bars this round.")
            return
        merged = dict(self.base_raw)
        merged.update(official)
        raw = [merged[k] for k in sorted(merged)]
        for tf, polled in completions.items():
            ok = needed[tf] in official
            if not ok:
                logger.warning(f"[{tf}m] Official candle for [{polled['time'].strftime('%H:%M')}] not published yet; "
                               f"using polled bar for it.")
            self.resamplers[tf].replace_closed(raw, polled, official_last=ok)

    def manage_positions(self):
        ltp_cache: dict[str, float | None] = {}
        for tf in list(self._open_tfs()):
            pos = self._pos(tf)
            if not pos:
                continue
            ikey = pos["instrument_key"]
            if ikey not in ltp_cache:
                ltp_cache[ikey] = self.client.get_option_ltp(ikey)
            self.manage_position(tf, ltp_cache[ikey])

    def manage_position(self, tf: int, opt_px: float | None):
        pos = self._pos(tf)
        if not pos or not opt_px:
            return
        cfg = self.cfg[tf]

        if opt_px > pos["peak_opt"]:
            pos["peak_opt"] = opt_px

        gain = (pos["peak_opt"] / pos["entry"]) - 1.0

        if gain >= (cfg["be_at"] / 100.0) and pos["current_sl"] < pos["entry"]:
            pos["current_sl"] = pos["entry"]
            logger.info(f"🛡️ [{tf}m] Stop moved to Breakeven @ ₹{pos['entry']:.2f}")

        if gain >= (cfg["trail_at"] / 100.0):
            profit = pos["peak_opt"] - pos["entry"]
            trail = pos["peak_opt"] - profit * (cfg["giveback"] / 100.0)
            if trail > pos["current_sl"]:
                pos["current_sl"] = trail
                logger.info(f"📈 [{tf}m] Trailing SL raised to ₹{pos['current_sl']:.2f}")

        if opt_px <= pos["current_sl"]:
            self.exit_trade(tf, f"Stop-Loss Hit (Trigger @ ₹{opt_px:.2f} <= SL ₹{pos['current_sl']:.2f})")
            return

        self._save_state()

    def process_signal(self, tf: int, candle: dict, now: datetime):
        resampler = self.resamplers[tf]
        closed = resampler.candles[:-1]
        if not closed or closed[-1]["time"] != candle["time"]:
            logger.error(f"[{tf}m] Closed-bar mismatch; skipping signal evaluation for this bar.")
            return

        res = evaluate_trendlines(closed, length=self.args.length, mult=1.0)

        if res["new_upper"]:
            orig = res["upper_origin"]
            logger.info(f"🟦 [{tf}m] NEW UPPER (TEAL) RESISTANCE LINE from {orig['time'].strftime('%H:%M')} "
                        f"@ ₹{orig['price']:.2f} | Current Level: ₹{res['upper']:.2f}")
        if res["new_lower"]:
            orig = res["lower_origin"]
            logger.info(f"🟥 [{tf}m] NEW LOWER (RED) SUPPORT LINE from {orig['time'].strftime('%H:%M')} "
                        f"@ ₹{orig['price']:.2f} | Current Level: ₹{res['lower']:.2f}")

        if self._pos(tf) is not None:
            return

        side = None
        trigger_type = None
        if res["up_break"]:
            side, trigger_type = "CE", "RESISTANCE BREAKOUT ('B')"
        elif res["support_bounce"]:
            side, trigger_type = "CE", "SUPPORT BOUNCE (GREEN LINE)"
        elif res["dn_break"]:
            side, trigger_type = "PE", "SUPPORT BREAKDOWN ('B')"
        elif res["resistance_reject"]:
            side, trigger_type = "PE", "RESISTANCE REJECTION (RED LINE)"

        if not side:
            return

        if self.quote_stale:
            logger.warning(f"[{tf}m] Signal {side} via {trigger_type} ignored: spot quote is frozen.")
            return

        if candle.get("stale"):
            logger.warning(f"[{tf}m] Signal {side} via {trigger_type} ignored: bar was built across a data gap.")
            return

        if not (MARKET_OPEN <= now.time() < ENTRY_CUTOFF):
            logger.info(f"[{tf}m] Signal ignored: outside entry window ({now.time()})")
            return

        contract = self.client.get_atm_contract(candle["close"], side)
        if not contract:
            logger.error(f"[{tf}m] No valid ATM option contract located.")
            return

        ikey, symbol = contract
        logger.info(f"🚀 [{tf}m] ==> SIGNAL DETECTED: {side} via {trigger_type} | Contract: {symbol}")
        order = self.client.place_order(ikey, "BUY", self.args.qty, slippage_pct=3.0, tag=f"lux{tf}m")

        if order["status"] == "UNKNOWN":
            self._halt(f"[{tf}m] BUY order state unknown for {symbol} (order_id={order.get('order_id')}).")

        if order["status"] in ("SUCCESS", "PARTIAL"):
            fill_px = order["fill_price"]
            qty = int(order.get("filled_qty") or self.args.qty)
            if order["status"] == "PARTIAL":
                logger.warning(f"[{tf}m] Partial entry fill: {qty}/{self.args.qty}. Managing the filled quantity only.")
            self.state["positions"][str(tf)] = {
                "timeframe": tf,
                "instrument_key": ikey,
                "symbol": symbol,
                "side": side,
                "trigger_type": trigger_type,
                "entry": fill_px,
                "qty": qty,
                "current_sl": fill_px * (1.0 - self.cfg[tf]["sl"] / 100.0),
                "peak_opt": fill_px,
            }
            self.state["trades_today"][str(tf)] += 1
            self._save_state()
            logger.info(f"🎯 [{tf}m] Position Opened: {symbol} x{qty} @ ₹{fill_px:.2f} | "
                        f"Initial SL: ₹{self.state['positions'][str(tf)]['current_sl']:.2f}")
        else:
            logger.error(f"❌ [{tf}m] Order execution failed for {symbol}.")

    def exit_trade(self, tf: int, reason: str):
        pos = self._pos(tf)
        if not pos:
            return
        if time.time() < self.next_exit_try[tf]:
            return

        logger.info(f"<== [{tf}m] EXITING POSITION: {pos['symbol']} | Reason: {reason}")

        for attempt, slip in enumerate((3.0, 6.0, 10.0), start=1):
            res = self.client.place_order(pos["instrument_key"], "SELL", pos["qty"],
                                          slippage_pct=slip, tag=f"lux{tf}m")
            status = res["status"]

            if status == "UNKNOWN":
                self._halt(f"[{tf}m] SELL order state unknown for {pos['symbol']} (order_id={res.get('order_id')}).")

            if status == "SUCCESS":
                logger.info(f"[{tf}m] Closed {pos['symbol']} successfully @ ₹{res['fill_price']:.2f}")
                self.state["positions"][str(tf)] = None
                self._save_state()
                return

            if status == "PARTIAL":
                filled = int(res.get("filled_qty") or 0)
                pos["qty"] -= filled
                self._save_state()
                logger.warning(f"[{tf}m] Partial exit fill ({filled}); {pos['qty']} qty remaining.")
                continue

            logger.error(f"[{tf}m] Exit attempt {attempt}/3 for {pos['symbol']} not filled.")

        self.next_exit_try[tf] = time.time() + 5
        logger.error(f"❌ [{tf}m] EXIT NOT CONFIRMED for {pos['symbol']} (qty {pos['qty']}). "
                     f"Position kept in state, will retry. CHECK YOUR BROKER APP.")


def main():
    parser = argparse.ArgumentParser(description="LuxAlgo Multi-Timeframe: Breaks & Bounces (3m + 5m + 15m)")
    parser.add_argument("--intervals", default="3,5,15",
                        help="Comma-separated candle intervals in minutes (default 3,5,15)")
    parser.add_argument("--length", type=int, default=14, help="Pivot lookback (14)")
    parser.add_argument("--mode", default="live", choices=["paper", "live"])
    parser.add_argument("--qty", type=int, default=65, help="Quantity PER timeframe position")
    parser.add_argument("--sl", default=None, help="Initial SL %% per timeframe, e.g. 3=15,5=20,15=25")
    parser.add_argument("--be-at", dest="be_at", default=None, help="Breakeven trigger %% per timeframe")
    parser.add_argument("--trail-at", dest="trail_at", default=None, help="Trail start %% per timeframe")
    parser.add_argument("--giveback", default=None, help="Trail giveback %% per timeframe")
    parser.add_argument("--fill-timeout", type=int, default=8, help="Seconds to wait for an order fill")
    parser.add_argument("--sync-wait", dest="sync_wait", type=float, default=20.0,
                        help="Max seconds to wait for the official 1-min candle after a bar closes")
    parser.add_argument("--paper-slippage", dest="paper_slippage", type=float, default=1.0,
                        help="Adverse slippage %% applied to paper fills")
    parser.add_argument("--token",
                        default=os.environ.get("UPSTOX_TOKEN") or os.environ.get("UPSTOX_ACCESS_TOKEN") or UPSTOX_ACCESS_TOKEN,
                        help="Upstox access token (env: UPSTOX_TOKEN preferred)")

    args = parser.parse_args()
    try:
        args.intervals = sorted({int(x) for x in args.intervals.split(",") if x.strip()})
    except ValueError:
        parser.error("--intervals must look like 3,5,15")
    if not args.intervals:
        parser.error("No intervals given.")
    if not args.token:
        parser.error(
            "No Upstox token found. Set the GitHub secret UPSTOX_TOKEN "
            "(or env UPSTOX_TOKEN / UPSTOX_ACCESS_TOKEN) or pass --token"
        )

    engine = MultiEngine(args)
    engine.run()


if __name__ == "__main__":
    main()
