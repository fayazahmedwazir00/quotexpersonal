"""
quotex_auth.py — Quotex login via WEB-based PIN (no Telegram)
"""
import builtins
import threading
import asyncio
import time
import re
import logging

from quotexapi.stable_api import Quotex

logger = logging.getLogger(__name__)

# ─── persistent event loop for all pyquotex async calls ───
_loop = asyncio.new_event_loop()

def _start_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

threading.Thread(target=_start_loop, daemon=True, name="qx-loop").start()


def run_async(coro, timeout=180):
    """Run coroutine on persistent loop and block for result."""
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    return fut.result(timeout=timeout)


# ─── input() patch for PIN prompt ───
_original_input = builtins.input
_patch_lock = threading.Lock()
_patch_state = {"active": False, "session_id": None}
_pending_pins = {}   # { session_id: {"event": Event, "ready": Event, "pin": str, "prompt": str} }

PIN_PROMPT_MARKER = "Insira o código PIN"  # pyquotex PT-BR prompt


def _input_wrapper(prompt=""):
    """Replacement for builtins.input, used by pyquotex for PIN prompt."""
    global _patch_state
    if PIN_PROMPT_MARKER in prompt and _patch_state.get("active"):
        sid = _patch_state.get("session_id")
        with _patch_lock:
            state = _pending_pins.get(sid)

        if state is None:
            logger.error("input_wrapper: no pending PIN slot for session %s", sid)
            return ""

        logger.info("input_wrapper: PIN prompt intercepted for session %s", sid)
        state["prompt"] = prompt
        state["ready"].set()   # tell Flask: PIN is needed → show PIN form

        # Block this thread until user submits PIN (or timeout)
        state["event"].wait(timeout=180)

        pin = state.get("pin")
        logger.info("input_wrapper: PIN received (len=%s)", len(pin) if pin else 0)
        return pin if pin else ""

    # not our prompt → behave normally
    return _original_input(prompt)


def _install_patch():
    builtins.input = _input_wrapper


def _prepare_pin_slot(session_id: str):
    _pending_pins[session_id] = {
        "event": threading.Event(),
        "ready": threading.Event(),
        "pin": None,
        "prompt": None,
    }
    _patch_state["active"] = True
    _patch_state["session_id"] = session_id


def _teardown_pin_slot(session_id: str):
    _patch_state["active"] = False
    _patch_state["session_id"] = None
    _pending_pins.pop(session_id, None)


def wait_for_pin_prompt(session_id: str, timeout: float = 45.0) -> bool:
    """Blocks until pyquotex actually asks for PIN (or timeout)."""
    state = _pending_pins.get(session_id)
    if state is None:
        return False
    return state["ready"].wait(timeout)


def submit_pin(session_id: str, pin: str) -> bool:
    """Called from Flask /submit-pin. Unblocks input_wrapper."""
    state = _pending_pins.get(session_id)
    if state is None:
        return False
    state["pin"] = pin.strip()
    state["event"].set()
    return True


# ─── active clients ───
_clients = {}   # { session_id: Quotex }


def get_client(session_id: str):
    return _clients.get(session_id)


def login_sync(session_id: str, email: str, password: str):
    """
    Blocking login. Returns (ok, reason, client).
    If PIN is required, this function blocks until submit_pin() is called.
    """
    _install_patch()
    _prepare_pin_slot(session_id)

    qx = None
    try:
        logger.info("Creating Quotex client for %s", email)
        qx = Quotex(email=email, password=password)
        _clients[session_id] = qx

        logger.info("Calling connect() — may block for PIN…")
        check, reason = run_async(qx.connect(), timeout=180)

        if check:
            logger.info("Quotex login OK for %s", email)
            return True, "OK", qx

        reason_str = str(reason) if reason else "unknown"
        logger.warning("Quotex login failed: %s", reason_str)
        _clients.pop(session_id, None)
        return False, reason_str, None

    except Exception as e:
        logger.exception("Login exception")
        if qx is not None:
            try:
                _clients.pop(session_id, None)
            except Exception:
                pass
        return False, f"{type(e).__name__}: {e}", None
    finally:
        _teardown_pin_slot(session_id)


# ─── helpers used by server.py ───

def change_account_mode(session_id: str, mode: str) -> bool:
    """mode must be 'PRACTICE' or 'REAL'."""
    qx = _clients.get(session_id)
    if qx is None:
        return False
    if mode not in ("PRACTICE", "REAL"):
        return False
    try:
        qx.change_account(mode)
        return True
    except Exception as e:
        logger.warning("change_account failed: %s", e)
        return False


def get_available_asset_sync(session_id: str, symbol: str):
    """Returns (resolved_symbol, info) or (None, None)."""
    qx = _clients.get(session_id)
    if qx is None:
        return None, None
    try:
        name, info = run_async(qx.get_available_asset(symbol, force_open=False), timeout=30)
        return name, info
    except Exception as e:
        logger.warning("get_available_asset failed: %s", e)
        return None, None


def fetch_candles_sync(session_id: str, symbol: str, period: int, count: int = 50):
    """
    Returns list of candles:
      [{ 'time': int, 'open': float, 'high': float, 'low': float, 'close': float }, …]
    """
    qx = _clients.get(session_id)
    if qx is None:
        return None

    end_time = int(time.time())
    offset = period * count

    try:
        raw = run_async(
            qx.get_candles(symbol, end_time, offset, period),
            timeout=45,
        )
    except Exception as e:
        logger.warning("get_candles failed: %s", e)
        return None

    if not raw:
        return None

    # Normalise to dicts with standard keys
    candles = []
    for c in raw:
        try:
            if isinstance(c, dict):
                candles.append({
                    "time":  int(c.get("time", c.get("from", 0))),
                    "open":  float(c["open"]),
                    "high":  float(c["high"]),
                    "low":   float(c["low"]),
                    "close": float(c["close"]),
                })
            else:  # some versions return tuples/lists
                candles.append({
                    "time":  int(c[0]),
                    "open":  float(c[1]),
                    "high":  float(c[2]),
                    "low":   float(c[3]),
                    "close": float(c[4]),
                })
        except Exception:
            continue

    # Drop incomplete last candle if it looks "fresh" (< 1 period old)
    if candles and (time.time() - candles[-1]["time"]) < period * 0.5:
        candles = candles[:-1]

    return candles or None


def place_trade_sync(session_id, symbol, direction, amount, duration, mode="TIMER"):
    """
    direction: 'CALL' or 'PUT'
    Returns dict with trade info or error.
    """
    qx = _clients.get(session_id)
    if qx is None:
        return {"error": "not logged in"}

    direction = direction.upper()
    if direction not in ("CALL", "PUT"):
        return {"error": f"invalid direction {direction}"}

    py_dir = "call" if direction == "CALL" else "put"

    try:
        status, info = run_async(
            qx.buy(amount, symbol, py_dir, duration, mode),
            timeout=30,
        )
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}

    if not status:
        return {"error": str(info)}

    trade_id = info.get("id") if isinstance(info, dict) else None
    return {
        "ok": True,
        "trade_id": trade_id,
        "symbol": symbol,
        "direction": direction,
        "amount": amount,
        "duration": duration,
        "mode": mode,
    }