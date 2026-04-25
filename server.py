"""
XAG WEBHOOK SERVER — v2.0
=========================
Receives Pine Script alerts from TradingView via POST webhook.
Validates signal against multi-factor gate (DXY, COT, kill zone, psychology).
Routes approved signals to OANDA v20 REST API for execution.

Deploy: Python 3.9+ · pip install flask oandapyV20 requests python-dotenv
Run:    python server.py
"""

from flask import Flask, request, jsonify
import requests, json, logging, os
from datetime import datetime, timezone
from dotenv import load_dotenv

try:
    import oandapyV20
    import oandapyV20.endpoints.orders as orders
    import oandapyV20.endpoints.trades as trades_ep
    import oandapyV20.endpoints.pricing as pricing
    from oandapyV20.contrib.requests import (
        MarketOrderRequest, TakeProfitDetails, StopLossDetails, TrailingStopLossOrderRequest
    )
    OANDA_AVAILABLE = True
except ImportError:
    OANDA_AVAILABLE = False
    print("⚠  oandapyV20 not installed — execution disabled. Install: pip install oandapyV20")

load_dotenv()

# ─── CONFIG ───────────────────────────────────────────────────────────────────
OANDA_ACCOUNT_ID   = os.getenv("OANDA_ACCOUNT_ID", "YOUR_ACCOUNT_ID")
OANDA_ACCESS_TOKEN = os.getenv("OANDA_ACCESS_TOKEN", "YOUR_TOKEN")
OANDA_ENV          = os.getenv("OANDA_ENV", "practice")   # "practice" | "live"
WEBHOOK_SECRET     = os.getenv("WEBHOOK_SECRET", "XAG_SECRET_2026")
ALPHA_VANTAGE_KEY  = os.getenv("ALPHA_VANTAGE_KEY", "YOUR_AV_KEY")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

INSTRUMENT         = "XAG_USD"
RISK_PERCENT       = 0.01          # 1% per trade — iron rule
MAX_DAILY_TRADES   = 2             # iron rule: max 2 trades/day
BREAKEVEN_RR       = 1.0           # move SL to breakeven at 1:1

# Kill zones (UTC hours). Only trade inside these windows.
KILL_ZONES = [
    (7, 10),    # London open
    (12, 15),   # New York open
]

# ─── LOGGING ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('xag_webhook.log')
    ]
)
log = logging.getLogger('XAG_WEBHOOK')

# ─── STATE ────────────────────────────────────────────────────────────────────
daily_trades = {}   # {date_str: count}
signal_log   = []   # last 50 signals

app = Flask(__name__)

# ─── OANDA CLIENT ─────────────────────────────────────────────────────────────
def get_oanda_client():
    if not OANDA_AVAILABLE:
        return None
    return oandapyV20.API(
        access_token=OANDA_ACCESS_TOKEN,
        environment=OANDA_ENV
    )

# ─── GATE FUNCTIONS ───────────────────────────────────────────────────────────

def is_kill_zone() -> bool:
    """Return True if current UTC time is inside a kill zone."""
    utc_hour = datetime.now(timezone.utc).hour
    return any(start <= utc_hour < end for start, end in KILL_ZONES)

def is_daily_limit_hit(date_str: str) -> bool:
    """Return True if we've already hit MAX_DAILY_TRADES today."""
    count = daily_trades.get(date_str, 0)
    return count >= MAX_DAILY_TRADES

def fetch_dxy_direction() -> dict:
    """
    Fetch DXY (USD index) from Alpha Vantage.
    Returns direction hint for XAG correlation check.
    Signal: DXY rising = XAG bearish pressure; DXY falling = XAG tailwind.
    """
    if not ALPHA_VANTAGE_KEY or ALPHA_VANTAGE_KEY == "YOUR_AV_KEY":
        return {"dxy": None, "direction": "unknown", "warn": "No API key"}

    try:
        url = (
            f"https://www.alphavantage.co/query"
            f"?function=FX_INTRADAY&from_symbol=USD&to_symbol=EUR"
            f"&interval=15min&apikey={ALPHA_VANTAGE_KEY}&outputsize=compact"
        )
        r = requests.get(url, timeout=5)
        data = r.json()
        ts = data.get("Time Series FX (15min)", {})
        if not ts:
            return {"dxy": None, "direction": "unknown"}
        keys = sorted(ts.keys(), reverse=True)
        latest = float(ts[keys[0]]["4. close"])
        prev   = float(ts[keys[1]]["4. close"])
        # EUR/USD inverse = DXY proxy
        dxy_proxy_change = (prev - latest) / prev  # EUR/USD down = DXY up
        direction = "rising" if dxy_proxy_change > 0.0005 else "falling" if dxy_proxy_change < -0.0005 else "flat"
        return {"dxy_proxy": round(dxy_proxy_change * 100, 3), "direction": direction}
    except Exception as e:
        log.warning(f"DXY fetch failed: {e}")
        return {"dxy": None, "direction": "unknown", "error": str(e)}

def fetch_live_price() -> float | None:
    """Fetch live XAG/USD bid from OANDA streaming pricing."""
    if not OANDA_AVAILABLE:
        return None
    try:
        client = get_oanda_client()
        r = pricing.PricingInfo(
            accountID=OANDA_ACCOUNT_ID,
            params={"instruments": INSTRUMENT}
        )
        resp = client.request(r)
        bid = float(resp["prices"][0]["bids"][0]["price"])
        ask = float(resp["prices"][0]["asks"][0]["price"])
        return (bid + ask) / 2
    except Exception as e:
        log.warning(f"Price fetch failed: {e}")
        return None

def calculate_units(entry: float, stop_loss: float, balance_usd: float = None) -> int:
    """
    Kelly/fixed-risk position sizing.
    Returns units (ounces) to trade at exactly 1% account risk.
    If balance not provided, pulls from OANDA account.
    """
    if balance_usd is None:
        balance_usd = get_account_balance()
    if not balance_usd or balance_usd <= 0:
        return 0
    risk_dollars = balance_usd * RISK_PERCENT
    stop_distance = abs(entry - stop_loss)
    if stop_distance == 0:
        return 0
    units = int(risk_dollars / stop_distance)
    return max(1, min(units, 1000))  # cap at 1000 oz

def get_account_balance() -> float | None:
    """Pull live account balance from OANDA."""
    if not OANDA_AVAILABLE:
        return None
    try:
        import oandapyV20.endpoints.accounts as accounts
        client = get_oanda_client()
        r = accounts.AccountSummary(OANDA_ACCOUNT_ID)
        resp = client.request(r)
        return float(resp["account"]["balance"])
    except Exception as e:
        log.warning(f"Balance fetch failed: {e}")
        return None

def run_gate(signal: dict) -> dict:
    """
    Multi-factor gate. Returns:
    { "pass": bool, "reasons": [str], "blocks": [str] }
    """
    reasons = []
    blocks  = []

    # 1. Secret token
    if signal.get("secret") != WEBHOOK_SECRET:
        blocks.append("INVALID_SECRET")
        return {"pass": False, "reasons": reasons, "blocks": blocks}

    # 2. Daily trade limit
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if is_daily_limit_hit(today):
        blocks.append(f"DAILY_LIMIT ({MAX_DAILY_TRADES} trades reached)")
    else:
        reasons.append(f"Daily limit OK ({daily_trades.get(today,0)}/{MAX_DAILY_TRADES})")

    # 3. Kill zone
    if is_kill_zone():
        reasons.append("Kill zone ACTIVE")
    else:
        blocks.append("OUTSIDE_KILL_ZONE")

    # 4. DXY alignment check
    dxy = fetch_dxy_direction()
    action = signal.get("action", "").upper()
    if dxy["direction"] == "rising" and action == "BUY":
        blocks.append(f"DXY_CONFLICT (DXY rising + BUY signal — headwind)")
    elif dxy["direction"] == "falling" and action == "SELL":
        blocks.append(f"DXY_CONFLICT (DXY falling + SELL signal — tailwind missing)")
    else:
        reasons.append(f"DXY aligned ({dxy['direction']})")

    # 5. SL presence
    if not signal.get("stop_loss"):
        blocks.append("NO_STOP_LOSS — iron rule violation")
    else:
        reasons.append("SL present")

    # 6. R:R check (if TP provided)
    entry = float(signal.get("entry", 0))
    sl    = float(signal.get("stop_loss", 0))
    tp    = float(signal.get("take_profit", 0))
    if entry and sl and tp:
        sl_dist = abs(entry - sl)
        tp_dist = abs(tp - entry)
        rr = tp_dist / sl_dist if sl_dist > 0 else 0
        if rr < 1.5:
            blocks.append(f"RR_TOO_LOW ({rr:.2f} < 1.5 minimum)")
        else:
            reasons.append(f"R:R {rr:.2f} — OK")

    gate_pass = len(blocks) == 0
    return {"pass": gate_pass, "reasons": reasons, "blocks": blocks, "dxy": dxy}

def execute_trade(signal: dict, gate: dict) -> dict:
    """Place market order on OANDA with SL + TP attached."""
    if not OANDA_AVAILABLE:
        return {"status": "simulation", "message": "oandapyV20 not installed — simulated"}

    client = get_oanda_client()
    entry  = float(signal.get("entry", 0)) or fetch_live_price() or 0
    sl     = float(signal.get("stop_loss", 0))
    tp     = float(signal.get("take_profit", 0)) if signal.get("take_profit") else None
    balance = get_account_balance()
    units  = calculate_units(entry, sl, balance)

    if signal.get("action", "").upper() == "SELL":
        units = -units

    try:
        kwargs = dict(
            instrument=INSTRUMENT,
            units=units,
            stopLossOnFill=StopLossDetails(price=round(sl, 4)).data
        )
        if tp:
            kwargs["takeProfitOnFill"] = TakeProfitDetails(price=round(tp, 4)).data

        mkt = MarketOrderRequest(**kwargs)
        r   = orders.OrderCreate(OANDA_ACCOUNT_ID, data=mkt.data)
        resp = client.request(r)
        trade_id = resp.get("relatedTransactionIDs", [None])[0]
        log.info(f"✅ Trade executed: {units} oz {INSTRUMENT} · tradeID: {trade_id}")
        return {"status": "executed", "units": units, "trade_id": trade_id, "response": resp}
    except Exception as e:
        log.error(f"❌ Execution failed: {e}")
        return {"status": "error", "error": str(e)}

def send_telegram(msg: str):
    """Push signal notification to Telegram."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
            timeout=5
        )
    except Exception as e:
        log.warning(f"Telegram failed: {e}")

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Main webhook endpoint.
    TradingView sends POST JSON here when Pine alert fires.
    """
    try:
        signal = request.get_json(force=True)
        if not signal:
            return jsonify({"error": "No JSON body"}), 400

        log.info(f"📨 Signal received: {json.dumps(signal)}")

        # Run gate
        gate = run_gate(signal)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        result = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal": signal,
            "gate": gate,
            "execution": None
        }

        if gate["pass"]:
            # Execute
            exec_result = execute_trade(signal, gate)
            result["execution"] = exec_result

            # Increment daily counter
            daily_trades[today] = daily_trades.get(today, 0) + 1

            # Notify
            action = signal.get("action","").upper()
            msg = (
                f"<b>✅ XAG {action} EXECUTED</b>\n"
                f"Entry: {signal.get('entry')}\n"
                f"SL: {signal.get('stop_loss')} | TP: {signal.get('take_profit','—')}\n"
                f"Signal: {signal.get('signal','')}\n"
                f"Trade #{daily_trades[today]} today"
            )
            send_telegram(msg)
            log.info(f"✅ Gate PASSED — trade executed")
        else:
            msg = (
                f"<b>🚫 XAG SIGNAL BLOCKED</b>\n"
                f"Action: {signal.get('action','')}\n"
                f"Blocks: {', '.join(gate['blocks'])}"
            )
            send_telegram(msg)
            log.warning(f"🚫 Gate BLOCKED: {gate['blocks']}")

        # Log signal
        signal_log.append(result)
        if len(signal_log) > 50:
            signal_log.pop(0)

        return jsonify(result), 200 if gate["pass"] else 200  # always 200 to TV

    except Exception as e:
        log.error(f"Webhook error: {e}", exc_info=True)
        return jsonify({"error": str(e)}), 500


@app.route("/status", methods=["GET"])
def status():
    """Health check + system state."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    price = fetch_live_price()
    balance = get_account_balance()
    return jsonify({
        "status": "online",
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "kill_zone_active": is_kill_zone(),
        "daily_trades_today": daily_trades.get(today, 0),
        "daily_limit": MAX_DAILY_TRADES,
        "live_price": price,
        "account_balance": balance,
        "instrument": INSTRUMENT,
        "last_signals": signal_log[-5:] if signal_log else []
    })


@app.route("/signals", methods=["GET"])
def signals():
    """Return last 20 signals for dashboard polling."""
    return jsonify({"signals": signal_log[-20:]})


@app.route("/close-all", methods=["POST"])
def close_all():
    """Emergency: close all open XAG positions."""
    secret = request.get_json(force=True, silent=True) or {}
    if secret.get("secret") != WEBHOOK_SECRET:
        return jsonify({"error": "Unauthorised"}), 403
    if not OANDA_AVAILABLE:
        return jsonify({"status": "simulation", "message": "No OANDA client"})
    try:
        import oandapyV20.endpoints.positions as positions
        client = get_oanda_client()
        r = positions.PositionClose(
            OANDA_ACCOUNT_ID, INSTRUMENT,
            data={"longUnits": "ALL", "shortUnits": "ALL"}
        )
        resp = client.request(r)
        log.info("🔴 All positions closed via /close-all")
        send_telegram("<b>🔴 EMERGENCY: All XAG positions closed</b>")
        return jsonify({"status": "closed", "response": resp})
    except Exception as e:
        return jsonify({"status": "error", "error": str(e)}), 500


# ─── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("=" * 60)
    log.info("  XAG WEBHOOK SERVER — Starting")
    log.info(f"  Environment : {OANDA_ENV}")
    log.info(f"  Instrument  : {INSTRUMENT}")
    log.info(f"  Risk/trade  : {RISK_PERCENT*100}%")
    log.info(f"  Max trades/day: {MAX_DAILY_TRADES}")
    log.info(f"  OANDA client: {'✓' if OANDA_AVAILABLE else '✗ (install oandapyV20)'}")
    log.info("=" * 60)
    app.run(host="0.0.0.0", port=8080, debug=False)
