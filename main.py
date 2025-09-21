# app.py
import os
import time
import hmac
import hashlib
import json
import asyncio
from typing import Dict, Any, Optional
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
import httpx
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mexc-bot")

# Config from env
DRY_RUN = os.getenv("DRY_RUN", "True").lower() in ("1","true","yes")
MEXC_API_KEY = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET", "")
LEVERAGE = float(os.getenv("LEVERAGE", "20"))
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "50"))
ALLOW_SLIPPAGE_PCT = float(os.getenv("ALLOW_SLIPPAGE_PCT", "0.30"))  # percent
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
TZ = os.getenv("TZ","Europe/Vienna")

# MEXC endpoints - spot/future endpoints vary; adjust if using linear perpetuals etc.
MEXC_BASE = "https://contract.mexc.com"  # adjust to your API (e.g., contract vs spot)

app = FastAPI(title="Signal2MEXC Bot")

class TelegramUpdate(BaseModel):
    update_id: int
    channel_post: Optional[Dict[str, Any]] = None
    message: Optional[Dict[str, Any]] = None

# ---------- Helpers ----------
def log_conf():
    logger.info(f"DRY_RUN={DRY_RUN}, LEVERAGE={LEVERAGE}, MARGIN_USDT={MARGIN_USDT}, TZ={TZ}")

def mexc_sign(params: Dict[str,str], secret: str) -> str:
    s = "&".join(f"{k}={v}" for k,v in sorted(params.items()))
    return hmac.new(secret.encode(), s.encode(), hashlib.sha256).hexdigest()

async def mexc_request(path: str, method="GET", params=None, json_body=None, auth=True):
    """Minimal wrapper. Adapt to official MEXC auth scheme. For safety: DRY_RUN simulates."""
    if DRY_RUN:
        logger.info("[DRY_RUN] mexc_request would call %s %s %s", method, path, params or json_body)
        return {"code": 200, "data": {"orderId": f"dry_{int(time.time())}"}}
    url = MEXC_BASE + path
    headers = {}
    if auth:
        ts = str(int(time.time()*1000))
        headers["ApiKey"] = MEXC_API_KEY
        sign_params = {"reqTime": ts, "apiKey": MEXC_API_KEY}
        sign = mexc_sign(sign_params, MEXC_API_SECRET)
        headers["Signature"] = sign
    async with httpx.AsyncClient(timeout=20) as client:
        if method.upper() == "GET":
            r = await client.get(url, params=params, headers=headers)
        else:
            r = await client.post(url, json=json_body, headers=headers)
        r.raise_for_status()
        return r.json()

# ---------- Trade logic ----------
def calc_quantity(price: float, margin_usdt: float, leverage: float) -> float:
    """For USDT linear perp: qty ≈ (margin * leverage) / price"""
    qty = (margin_usdt * leverage) / price
    return float("{:.6f}".format(qty))  # TODO: round to symbol lot step

async def open_position(symbol: str, side: str, entry_price: float, sl_price: Optional[float], tp1_price: float, tp2_price: float):
    """
    Open position with MARGIN_USDT & LEVERAGE, set TP1 (30%) + TP2 (70%), SL initial.
    On TP1 fill -> move SL to break-even (notified via polling/websocket in real impl.).
    """
    side_up = side.upper()
    qty = calc_quantity(entry_price, MARGIN_USDT, LEVERAGE)
    logger.info("Opening %s %s qty=%.6f @ %.6f", side_up, symbol, qty, entry_price)

    open_payload = {
        "symbol": symbol,
        "side": side_up,
        "type": "MARKET",
        "quantity": qty,
        "leverage": LEVERAGE,
    }
    resp = await mexc_request("/api/v1/private/order", method="POST", json_body=open_payload, auth=True)
    order_id = resp.get("data", {}).get("orderId", f"dry_{int(time.time())}")
    logger.info("Open order_id=%s", order_id)

    # Opposite side for TPs/SL
    close_side = "SELL" if side_up in ("BUY","LONG") else "BUY"

    tp1_qty = float("{:.6f}".format(qty * 0.30))
    tp2_qty = float("{:.6f}".format(qty - tp1_qty))

    # TP1
    tp1_payload = {
        "symbol": symbol,
        "side": close_side,
        "type": "LIMIT",
        "price": tp1_price,
        "quantity": tp1_qty,
        "reduceOnly": True
    }
    tp1_resp = await mexc_request("/api/v1/private/order", method="POST", json_body=tp1_payload, auth=True)
    tp1_id = tp1_resp.get("data", {}).get("orderId", f"dry_tp1_{int(time.time())}")
    logger.info("Placed TP1 id=%s qty=%.6f price=%s", tp1_id, tp1_qty, tp1_price)

    # TP2
    tp2_payload = {
        "symbol": symbol,
        "side": close_side,
        "type": "LIMIT",
        "price": tp2_price,
        "quantity": tp2_qty,
        "reduceOnly": True
    }
    tp2_resp = await mexc_request("/api/v1/private/order", method="POST", json_body=tp2_payload, auth=True)
    tp2_id = tp2_resp.get("data", {}).get("orderId", f"dry_tp2_{int(time.time())}")
    logger.info("Placed TP2 id=%s qty=%.6f price=%s", tp2_id, tp2_qty, tp2_price)

    # initial SL
    sl_id = None
    if sl_price:
        sl_payload = {
            "symbol": symbol,
            "side": close_side,
            "type": "STOP_MARKET",
            "stopPrice": sl_price,
            "quantity": float("{:.6f}".format(qty)),
            "reduceOnly": True
        }
        sl_resp = await mexc_request("/api/v1/private/order", method="POST", json_body=sl_payload, auth=True)
        sl_id = sl_resp.get("data", {}).get("orderId", f"dry_sl_{int(time.time())}")
        logger.info("Placed SL id=%s stopPrice=%s", sl_id, sl_price)

    return {"open_id": order_id, "tp1_id": tp1_id, "tp2_id": tp2_id, "sl_id": sl_id, "quantity": qty, "entry_price": entry_price}

async def poll_order_status(order_id: str, wait=1, attempts=30):
    """Polling stub. Replace with real GET order endpoint or WebSocket fills."""
    for _ in range(attempts):
        resp = await mexc_request(f"/api/v1/private/order/{order_id}", method="GET", auth=True)
        data = resp.get("data", {})
        status = data.get("status", "FILLED" if DRY_RUN else "OPEN")
        logger.info("Order %s status=%s", order_id, status)
        if status.upper() in ("FILLED","CANCELED","REJECTED"):
            return data
        await asyncio.sleep(wait)
    return {"status":"UNKNOWN"}

# ---------- Telegram webhook ----------
@app.post("/telegram/webhook")
async def telegram_webhook(update: TelegramUpdate, request: Request):
    """Receives Telegram updates (channel_post or message)."""
    raw = await request.json()
    logger.info("Telegram update: %s", json.dumps(raw)[:1000])

    text = None
    if update.channel_post:
        text = update.channel_post.get("text") or update.channel_post.get("caption")
    elif update.message:
        text = update.message.get("text") or update.message.get("caption")
    if not text:
        logger.info("No text in update - ignoring")
        return {"ok": True}

    parsed = parse_signal(text)
    if not parsed:
        logger.info("Could not parse signal: %s", text)
        return {"ok": True, "note": "unparsed"}

    result = await open_position(
        symbol=parsed["symbol"],
        side=parsed["side"],
        entry_price=parsed["price"],
        sl_price=parsed.get("sl"),
        tp1_price=parsed["tp1"],
        tp2_price=parsed["tp2"]
    )
    logger.info("Position opened: %s", result)
    return {"ok": True, "result": result}

def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    """Accepts e.g.: 'BUY BTCUSDT @42000 TP1=42300 TP2=43000 SL=41000'"""
    t = text.replace(",", " ").replace("\n"," ").upper()
    tokens = t.split()
    # side
    side = "BUY" if ("BUY" in tokens or "LONG" in tokens) else ("SELL" if ("SELL" in tokens or "SHORT" in tokens) else None)
    # symbol
    symbol = next((tok for tok in tokens if tok.endswith("USDT")), None)
    # price (after @)
    price = None
    for tok in tokens:
        if tok.startswith("@"):
            try:
                price = float(tok.strip("@"))
                break
            except:
                pass
    # TP/SL
    tp1 = tp2 = sl = None
    for tok in tokens:
        if tok.startswith("TP1=") or tok.startswith("TP1:"):
            try: tp1 = float(tok.split("=")[1] if "=" in tok else tok.split(":")[1])
            except: pass
        if tok.startswith("TP2=") or tok.startswith("TP2:"):
            try: tp2 = float(tok.split("=")[1] if "=" in tok else tok.split(":")[1])
            except: pass
        if tok.startswith("SL=") or tok.startswith("SL:"):
            try: sl = float(tok.split("=")[1] if "=" in tok else tok.split(":")[1])
            except: pass
    if not price and tp1 and tp2:
        price = (tp1 + tp2) / 2.0
    if not (side and symbol and tp1 and tp2 and price):
        return None
    return {"side": side, "symbol": symbol, "price": price, "tp1": tp1, "tp2": tp2, "sl": sl}

# ---------- Health & test ----------
@app.get("/health")
async def health():
    return {"status":"ok", "dry_run": DRY_RUN}

@app.post("/test_signal")
async def test_signal(payload: Dict[str, Any]):
    required = ["symbol","side","price","tp1","tp2"]
    if not all(k in payload for k in required):
        raise HTTPException(status_code=400, detail=f"missing keys, required {required}")
    res = await open_position(
        symbol=payload["symbol"],
        side=payload["side"],
        entry_price=float(payload["price"]),
        sl_price=float(payload.get("sl")) if payload.get("sl") else None,
        tp1_price=float(payload["tp1"]),
        tp2_price=float(payload["tp2"])
    )
    return {"ok": True, "result": res}

# ---------- Startup ----------
@app.on_event("startup")
async def startup_event():
    log_conf()
    logger.info("Bot starting up. Use /health and /test_signal for quick checks.")
