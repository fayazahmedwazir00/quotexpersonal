"""
analyzer.py — 8-model AI ensemble + weighted voting
"""
import json
import re
import logging
import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

logger = logging.getLogger(__name__)

# ─── AI endpoint (آپ کا موجودہ api.php deployment) ───
AI_ENDPOINT = "https://ultrafast.vercel.app"
AI_TIMEOUT  = 90

# ─── 8 ensemble models + weights ───
MODELS = [
    {"id": "deepseek-v4-flash", "name": "DeepSeek V4 Flash", "short": "DSF", "weight": 1.0},
    {"id": "qwen3.7-max",       "name": "Qwen 3.7 Max",      "short": "Q3M", "weight": 1.1},
    {"id": "glm-5.2",           "name": "GLM 5.2",           "short": "GLM", "weight": 1.0},
    {"id": "minimax-m3",        "name": "MiniMax M3",        "short": "MM3", "weight": 0.9},
    {"id": "qwen3-235b",        "name": "Qwen 3 235B",       "short": "Q23", "weight": 1.1},
    {"id": "deepseek-v3",       "name": "DeepSeek V3",       "short": "DS3", "weight": 1.2},
    {"id": "qwen3-235b-tput",   "name": "Qwen 3 235B TPUT",  "short": "Q2T", "weight": 0.9},
    {"id": "llama-3.3-70b",     "name": "Llama 3.3 70B",     "short": "L33", "weight": 0.8},
]


# ─── System prompt (shared by all 8) ───
SYSTEM_PROMPT = """You are a professional binary-options market analyst.

You will receive structured market data (JSON) for ONE symbol: last 50 candles +
technical indicators (RSI, EMA20, EMA50, MACD, Bollinger, ATR, support/resistance).

Your job: predict the direction of the NEXT candle (1 timeframe forward).

ANALYSIS RULES (apply strictly):
- RSI < 30 → oversold → CALL bias
- RSI > 70 → overbought → PUT bias
- Price above BOTH EMA20 and EMA50 → uptrend → CALL bias
- Price below BOTH EMA20 and EMA50 → downtrend → PUT bias
- MACD histogram > 0 and rising → bullish momentum → CALL
- MACD histogram < 0 and falling → bearish momentum → PUT
- Price crossing Bollinger upper → possible reversal → PUT
- Price crossing Bollinger lower → possible reversal → CALL
- ATR high (volatility) → reduce confidence
- Recent 3-candle trend matters more than older candles

If signals CONFLICT or data is weak → return NEUTRAL with low confidence.

STRICT OUTPUT — return ONLY this JSON, no markdown, no explanation outside JSON:

{
  "decision": "CALL" | "PUT" | "NEUTRAL",
  "confidence": <integer 0-100>,
  "reason": "<one short sentence, max 100 chars>"
}

Do NOT wrap in code fences. Do NOT write anything else.
"""


# ─── Indicator math (pure Python, no numpy) ───
def _ema(values, period):
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(values[:period]) / period
    for v in values[period:]:
        val = v * k + val * (1 - k)
    return val


def _ema_series(values, period):
    if len(values) < period:
        return []
    k = 2.0 / (period + 1)
    out = [sum(values[:period]) / period]
    for v in values[period:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(d if d > 0 else 0.0)
        losses.append(-d if d < 0 else 0.0)
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_g = (avg_g * (period - 1) + gains[i]) / period
        avg_l = (avg_l * (period - 1) + losses[i]) / period
    if avg_l == 0:
        return 100.0
    rs = avg_g / avg_l
    return 100.0 - (100.0 / (1.0 + rs))


def _macd(closes, fast=12, slow=26, signal=9):
    if len(closes) < slow + signal:
        return None
    ema_f = _ema_series(closes, fast)
    ema_s = _ema_series(closes, slow)
    if not ema_f or not ema_s:
        return None
    # align: ema_s starts (fast) later
    diff_len = len(ema_f) - len(ema_s)
    ema_f = ema_f[diff_len:]
    macd_line = [a - b for a, b in zip(ema_f, ema_s)]
    if len(macd_line) < signal:
        return None
    sig_line = _ema_series(macd_line, signal)
    if not sig_line:
        return None
    return {
        "macd": round(macd_line[-1], 6),
        "signal": round(sig_line[-1], 6),
        "histogram": round(macd_line[-1] - sig_line[-1], 6),
    }


def _bollinger(closes, period=20, mult=2.0):
    if len(closes) < period:
        return None
    recent = closes[-period:]
    mid = sum(recent) / period
    var = sum((x - mid) ** 2 for x in recent) / period
    sd = var ** 0.5
    return {
        "upper": round(mid + mult * sd, 6),
        "middle": round(mid, 6),
        "lower": round(mid - mult * sd, 6),
        "bandwidth": round((2 * mult * sd) / mid * 100, 4) if mid else 0,
    }


def _atr(candles, period=14):
    if len(candles) < period + 1:
        return None
    trs = []
    for i in range(1, len(candles)):
        h = candles[i]["high"]
        l = candles[i]["low"]
        pc = candles[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return round(sum(trs[-period:]) / period, 6)


def _support_resistance(candles, window=20):
    if len(candles) < window:
        return [], []
    highs = [c["high"] for c in candles[-window:]]
    lows = [c["low"] for c in candles[-window:]]
    return [round(min(lows), 6)], [round(max(highs), 6)]


def _trend_label(closes):
    if len(closes) < 50:
        return "unknown"
    e20 = _ema(closes, 20)
    e50 = _ema(closes, 50)
    price = closes[-1]
    if e20 is None or e50 is None:
        return "unknown"
    if price > e20 > e50:
        return "strong_bullish"
    if price > e20 and price > e50:
        return "slight_bullish"
    if price < e20 < e50:
        return "strong_bearish"
    if price < e20 and price < e50:
        return "slight_bearish"
    return "sideways"


# ─── Market data builder ───
def build_market_data(symbol: str, timeframe_seconds: int, candles: list) -> dict:
    closes = [c["close"] for c in candles]
    if len(closes) < 20:
        return {"symbol": symbol, "error": "insufficient data", "candles_count": len(closes)}

    last5 = candles[-5:]
    directions = []
    for c in last5:
        if c["close"] > c["open"]:
            directions.append("up")
        elif c["close"] < c["open"]:
            directions.append("down")
        else:
            directions.append("flat")

    support, resistance = _support_resistance(candles)

    data = {
        "symbol": symbol,
        "timeframe_seconds": timeframe_seconds,
        "current_price": round(closes[-1], 6),
        "candles_count": len(candles),
        "candles": [
            {
                "t": c["time"],
                "o": round(c["open"], 6),
                "h": round(c["high"], 6),
                "l": round(c["low"], 6),
                "c": round(c["close"], 6),
            }
            for c in candles[-30:]     # last 30 → enough & keeps prompt small
        ],
        "indicators": {
            "rsi_14": round(_rsi(closes, 14) or 0, 2),
            "ema_20": round(_ema(closes, 20) or 0, 6),
            "ema_50": round(_ema(closes, 50) or 0, 6),
            "macd": _macd(closes),
            "bollinger": _bollinger(closes),
            "atr_14": _atr(candles),
            "trend": _trend_label(closes),
            "support": support,
            "resistance": resistance,
            "last_5_directions": directions,
        },
    }
    return data


# ─── Single AI call (streaming SSE parse) ───
def _call_model(model_id: str, market_data: dict) -> dict:
    user_msg = (
        "Analyze the following market data and return ONLY the required JSON:\n\n"
        + json.dumps(market_data, separators=(",", ":"))
    )
    payload = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",   "content": user_msg},
        ],
    }

    text = ""
    try:
        with requests.post(AI_ENDPOINT, json=payload,
                           timeout=AI_TIMEOUT, stream=True) as r:
            if r.status_code != 200:
                return {"model": model_id, "error": f"HTTP {r.status_code}"}

            for raw_line in r.iter_lines(decode_unicode=True):
                if not raw_line:
                    continue
                line = raw_line.strip()
                if not line.startswith("data:"):
                    continue
                chunk = line[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                if "error" in obj:
                    return {"model": model_id, "error": str(obj["error"])}
                if "token" in obj:
                    text += obj["token"]
    except requests.RequestException as e:
        return {"model": model_id, "error": f"request: {e}"}

    parsed = _parse_ai_json(text)
    if parsed is None:
        return {"model": model_id, "error": "unparseable response",
                "raw": text[:300]}

    decision = str(parsed.get("decision", "NEUTRAL")).upper()
    if decision not in ("CALL", "PUT", "NEUTRAL"):
        decision = "NEUTRAL"
    try:
        conf = int(parsed.get("confidence", 0))
    except Exception:
        conf = 0
    conf = max(0, min(100, conf))

    return {
        "model": model_id,
        "decision": decision,
        "confidence": conf,
        "reason": str(parsed.get("reason", ""))[:140],
    }


def _parse_ai_json(text: str):
    if not text:
        return None
    s = text.strip()
    # strip code fences if any
    s = re.sub(r"^```(?:json)?\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    # last resort: grab first {...}
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return None


# ─── Ensemble runner ───
def run_ensemble(market_data: dict) -> list:
    """Runs all 8 models in parallel. Returns list of per-model results."""
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_call_model, m["id"], market_data): m for m in MODELS}
        for fut in as_completed(futures):
            m = futures[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {"model": m["id"], "error": str(e)}
            r["name"] = m["name"]
            r["short"] = m["short"]
            r["weight"] = m["weight"]
            results.append(r)

    # order results to match MODELS order for stable UI
    order = {m["id"]: i for i, m in enumerate(MODELS)}
    results.sort(key=lambda r: order.get(r["model"], 99))
    return results


# ─── Weighted voting ───
def weighted_vote(results: list) -> tuple:
    """
    Returns (decision, confidence_pct, votes_breakdown)
      decision: 'CALL' | 'PUT' | 'NO_TRADE'
      confidence_pct: float 0-100
      votes_breakdown: dict with per-side counts + weighted scores
    """
    call_w = put_w = neut_w = 0.0
    total_w = 0.0
    call_models = []
    put_models = []
    neut_models = []
    failed = []

    for r in results:
        if r.get("error"):
            failed.append(r["model"])
            continue
        w = float(r.get("weight", 1.0)) * (int(r.get("confidence", 0)) / 100.0)
        total_w += w
        d = r.get("decision", "NEUTRAL")
        if d == "CALL":
            call_w += w
            call_models.append(r["short"])
        elif d == "PUT":
            put_w += w
            put_models.append(r["short"])
        else:
            neut_w += w
            neut_models.append(r["short"])

    if total_w == 0:
        return "NO_TRADE", 0.0, {
            "call_models": [], "put_models": [], "neutral_models": neut_models,
            "failed_models": failed, "call_pct": 0, "put_pct": 0, "neutral_pct": 100,
        }

    call_pct = call_w / total_w * 100.0
    put_pct  = put_w  / total_w * 100.0
    neut_pct = neut_w / total_w * 100.0

    votes = {
        "call_models": call_models,
        "put_models": put_models,
        "neutral_models": neut_models,
        "failed_models": failed,
        "call_pct": round(call_pct, 1),
        "put_pct": round(put_pct, 1),
        "neutral_pct": round(neut_pct, 1),
    }

    # decision thresholds
    if call_pct >= 55 and call_pct >= put_pct + 15:
        return "CALL", round(call_pct, 1), votes
    if put_pct >= 55 and put_pct >= call_pct + 15:
        return "PUT", round(put_pct, 1), votes

    return "NO_TRADE", round(max(call_pct, put_pct), 1), votes