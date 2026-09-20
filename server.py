"""
server.py — Flask API for Quotex AI Analyzer
"""
import os
import uuid
import time
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask, request, jsonify, send_from_directory

import quotex_auth
import analyzer
import database

# ─── logging ───
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("server")

# ─── Flask ───
app = Flask(__name__, static_folder=".", static_url_path="")
executor = ThreadPoolExecutor(max_workers=6)

# ─── CORS ───
@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp

@app.route("/<path:_>", methods=["OPTIONS"])
def _options(_):
    return ("", 204)

# ─── static ───
@app.route("/", methods=["GET"])
def root():
    return send_from_directory(".", "index.html")

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True, "time": int(time.time())})


# ─── symbols list ───
SYMBOLS = [
    "EURUSD", "EURUSD_otc",
    "GBPUSD", "GBPUSD_otc",
    "USDJPY", "USDJPY_otc",
    "AUDUSD", "AUDUSD_otc",
    "USDCAD", "USDCAD_otc",
    "USDCHF", "USDCHF_otc",
    "NZDUSD", "NZDUSD_otc",
    "EURGBP", "EURGBP_otc",
    "EURJPY", "EURJPY_otc",
    "GBPJPY", "GBPJPY_otc",
    "AUDJPY", "AUDJPY_otc",
    "CADJPY", "CADJPY_otc",
    "CHFJPY", "CHFJPY_otc",
    "EURAUD", "EURAUD_otc",
    "EURCAD", "EURCAD_otc",
    "GBPAUD", "GBPAUD_otc",
    "XAUUSD", "XAUUSD_otc",
    "XAGUSD", "XAGUSD_otc",
    "BTCUSD", "BTCUSD_otc",
    "ETHUSD", "ETHUSD_otc",
]

TIMEFRAMES = [
    {"label": "1m",  "seconds": 60},
    {"label": "2m",  "seconds": 120},
    {"label": "5m",  "seconds": 300},
    {"label": "15m", "seconds": 900},
]

@app.route("/symbols", methods=["GET"])
def symbols():
    return jsonify({"symbols": SYMBOLS, "timeframes": TIMEFRAMES})


# ─── login ───
@app.route("/login", methods=["POST"])
def login():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = (body.get("password") or "").strip()

    if not email or not password:
        return jsonify({"error": "Email and password required"}), 400

    session_id = uuid.uuid4().hex
    logger.info("Login start for %s (session=%s)", email, session_id[:8])

    # kick off login in a worker thread (it may block on PIN)
    fut = executor.submit(quotex_auth.login_sync, session_id, email, password)

    # wait briefly to see if PIN is required
    pin_needed = quotex_auth.wait_for_pin_prompt(session_id, timeout=45)

    if pin_needed:
        logger.info("PIN required for session %s", session_id[:8])
        return jsonify({
            "session_id": session_id,
            "need_pin": True,
            "message": "PIN sent to your email. Enter it to continue.",
        })

    # no PIN → wait for login to finish
    try:
        ok, reason, _client = fut.result(timeout=90)
    except Exception as e:
        return jsonify({"error": f"{type(e).__name__}: {e}"}), 500

    if ok:
        database.save_session(session_id, email)
        return jsonify({
            "session_id": session_id,
            "logged_in": True,
            "email": email,
        })

    return jsonify({
        "error": reason or "Login failed",
        "session_id": session_id,
        "logged_in": False,
    }), 401


# ─── PIN submit ───
@app.route("/submit-pin", methods=["POST"])
def submit_pin():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    pin = body.get("pin")
    if not session_id or not pin:
        return jsonify({"error": "session_id and pin required"}), 400

    ok = quotex_auth.submit_pin(session_id, pin)
    if not ok:
        return jsonify({"error": "Invalid session or PIN already submitted"}), 400

    logger.info("PIN submitted for session %s", session_id[:8])
    return jsonify({"ok": True})


# ─── wait for login to complete (after PIN) ───
@app.route("/login-status", methods=["POST"])
def login_status():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    client = quotex_auth.get_client(session_id)
    if client is None:
        return jsonify({"logged_in": False, "reason": "client not found"})

    if getattr(client, "check_connect", False):
        sess = database.get_session(session_id)
        return jsonify({
            "logged_in": True,
            "email": sess["email"] if sess else None,
            "mode": sess["mode"] if sess else "PRACTICE",
        })

    return jsonify({"logged_in": False, "reason": "not connected yet"})


# ─── change account mode ───
@app.route("/set-mode", methods=["POST"])
def set_mode():
    body = request.get_json(silent=True) or {}
    session_id = body.get("session_id")
    mode = (body.get("mode") or "").upper()
    if mode not in ("PRACTICE", "REAL"):
        return jsonify({"error": "mode must be PRACTICE or REAL"}), 400

    ok = quotex_auth.change_account_mode(session_id, mode)
    if ok:
        database.update_session_mode(session_id, mode)
    return jsonify({"ok": ok, "mode": mode})


# ─── analyze ───
@app.route("/analyze", methods=["POST"])
def analyze():
    body = request.get_json(silent=True) or {}
    session_id   = body.get("session_id")
    symbol       = (body.get("symbol") or "").strip()
    timeframe    = int(body.get("timeframe") or 60)
    auto_trade   = bool(body.get("auto_trade", False))
    trade_mode   = (body.get("trade_mode") or "TIMER").upper()

    if not session_id or not symbol:
        return jsonify({"error": "session_id and symbol required"}), 400

    if timeframe not in (60, 120, 300, 900):
        return jsonify({"error": "invalid timeframe"}), 400

    # 1. session check
    client = quotex_auth.get_client(session_id)
    if client is None or not getattr(client, "check_connect", False):
        return jsonify({"error": "Not logged in. Please login first.",
                        "decision": "NO_TRADE"}), 401

    database.touch_session(session_id)

    # 2. symbol availability — returns actual symbol + warning if variant used
    resolved, info = quotex_auth.get_available_asset_sync(session_id, symbol)
    warning = None
    if resolved is None:
        return jsonify({
            "symbol": symbol,
            "decision": "NO_TRADE",
            "error": f"Symbol '{symbol}' not found in Quotex.",
            "warning": "Make sure you spelled the symbol correctly and it exists.",
        }), 200

    if resolved != symbol:
        warning = (f"Symbol auto-resolved: requested '{symbol}' → using '{resolved}'.")

    # 3. candles
    candles = quotex_auth.fetch_candles_sync(session_id, resolved, timeframe, count=60)
    if not candles or len(candles) < 20:
        return jsonify({
            "symbol": resolved,
            "decision": "NO_TRADE",
            "error": f"Insufficient candle data ({0 if not candles else len(candles)} candles).",
            "warning": "Symbol may be closed or low liquidity. Try another symbol.",
        }), 200

    # 4. market data
    market_data = analyzer.build_market_data(resolved, timeframe, candles)
    if "error" in market_data:
        return jsonify({
            "symbol": resolved,
            "decision": "NO_TRADE",
            "error": market_data["error"],
        }), 200

    # 5. ensemble
    logger.info("Running 8-model ensemble for %s (%ss)…", resolved, timeframe)
    t0 = time.time()
    results = analyzer.run_ensemble(market_data)
    elapsed_ms = int((time.time() - t0) * 1000)
    logger.info("Ensemble done in %dms", elapsed_ms)

    # 6. voting
    decision, confidence, votes = analyzer.weighted_vote(results)

    # 7. save history
    try:
        database.save_history(resolved, timeframe, decision, confidence, votes, market_data)
    except Exception as e:
        logger.warning("history save failed: %s", e)

    # 8. optional auto-trade
    trade_result = None
    if auto_trade and decision in ("CALL", "PUT") and confidence >= 60:
        logger.info("Auto-trade ON → placing %s on %s", decision, resolved)
        trade_result = quotex_auth.place_trade_sync(
            session_id, resolved, decision,
            amount=1, duration=timeframe, mode=trade_mode,
        )

    # 9. response
    return jsonify({
        "symbol": resolved,
        "requested_symbol": symbol,
        "timeframe": timeframe,
        "decision": decision,
        "confidence": confidence,
        "votes": votes,
        "market_data": {
            "current_price": market_data.get("current_price"),
            "indicators": market_data.get("indicators"),
        },
        "model_results": [
            {
                "name": r.get("name"),
                "short": r.get("short"),
                "decision": r.get("decision"),
                "confidence": r.get("confidence"),
                "reason": r.get("reason"),
                "error": r.get("error"),
            }
            for r in results
        ],
        "warning": warning,
        "trade_result": trade_result,
        "elapsed_ms": elapsed_ms,
    })


# ─── history ───
@app.route("/history", methods=["GET"])
def history():
    return jsonify({"items": database.get_history(limit=50)})


# ─── main ───
if __name__ == "__main__":
    database.init_db()
    logger.info("Starting Flask server on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, threaded=True, debug=False)