import os, asyncio, math, time, re, uuid, logging
from typing import Optional, Dict, Any

import ccxt
from fastapi import FastAPI
from telegram import Update, Bot
from telegram.ext import Application, AIORateLimiter, MessageHandler, filters

# =====================
#   ENV VARS (REQUIRED)
# =====================
TG_TOKEN        = os.getenv("TG_TOKEN")
SOURCE_CHAT_ID  = int(os.getenv("SOURCE_CHAT_ID", "0"))   # Kanal/Gruppen-ID der Signale
DEST_CHAT_ID    = int(os.getenv("DEST_CHAT_ID", "0"))     # dein privater Chat
MEXC_API_KEY    = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET = os.getenv("MEXC_API_SECRET")

# =====================
#   PARAMS (ENV-override)
# =====================
LEVERAGE  = int(os.getenv("LEVERAGE", "20"))
MARGIN_USDT = float(os.getenv("MARGIN_USDT", "30"))
ISOLATED  = os.getenv("ISOLATED", "true").lower() == "true"
MAX_ENTRY_DEVIATION_PCT = float(os.getenv("MAX_ENTRY_DEV_PCT", "0.30"))
POST_FILLS = os.getenv("POST_FILLS", "true").lower() == "true"
POLL_INTERVAL_S = int(os.getenv("POLL_INTERVAL_S", "7"))

# ====== Logging ======
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("executor")

# ====== FastAPI ======
app = FastAPI(title="MEXC Futures Executor Bot")

status_state: Dict[str, Any] = {
    "ok": False,
    "last_signal": None,
    "last_order_info": {},
    "running": False,
    "exchange_ok": False,
    "err": None,
}

# ====== Telegram & Exchange (lazy init) ======
tg_app: Optional[Application] = None
tg_bot: Optional[Bot] = None
ex: Optional[ccxt.Exchange] = None

# ---------- Helpers ----------
def to_swap_symbol(symbol_spot: str) -> str:
    if symbol_spot.endswith("/USDT"):
        base = symbol_spot.split("/")[0]
        return f"{base}/USDT:USDT"
    return symbol_spot

SIG_RE = re.compile(
    r"STRIKT.*?—\s*(?P<symbol>[A-Z0-9/]+)\s+[0-9a-zA-Z]+\s+"
    r"➡️\s*\*(?P<side>LONG|SHORT)\*\s*.*?"
    r"Entry:\s*`(?P<entry>[0-9.]+)`.*?"
    r"TP1:\s*`(?P<tp1>[0-9.]+)`.*?"
    r"TP2:\s*`(?P<tp2>[0-9.]+)`.*?"
    r"(TP3:\s*`(?P<tp3>[0-9.]+)`.*?)?"
    r"SL:\s*`(?P<sl>[0-9.]+)`",
    re.S | re.I
)

async def post(msg: str):
    if tg_bot and DEST_CHAT_ID:
        try:
            await tg_bot.send_message(chat_id=DEST_CHAT_ID, text=msg)
        except Exception as e:
            log.exception("Telegram send failed: %s", e)

def within_dev(cur: float, ref: float, max_pct: float) -> bool:
    dev = abs(cur - ref) / ref * 100
    return dev <= max_pct

def calc_qty(symbol: str, price: float) -> float:
    assert ex is not None
    m = ex.market(symbol)
    notional = MARGIN_USDT * LEVERAGE
    raw_qty = notional / max(price, 1e-9)
    qty = ex.amount_to_precision(symbol, raw_qty)
    min_amt = m["limits"]["amount"].get("min") or 0
    if min_amt and float(qty) < float(min_amt):
        qty = ex.amount_to_precision(symbol, float(min_amt))
    return float(qty)

async def set_symbol_params(symbol: str):
    assert ex is not None
    try:
        ex.set_leverage(LEVERAGE, symbol, params={"marginMode": "isolated" if ISOLATED else "cross"})
        try:
            ex.set_margin_mode("isolated" if ISOLATED else "cross", symbol=symbol)
        except Exception:
            pass
    except Exception as e:
        log.warning("Leverage/MarginMode für %s nicht gesetzt: %s", symbol, e)

# ---------- Core trading ----------
async def place_trade_from_signal(sig: Dict[str, Any]):
    assert ex is not None
    status_state["last_signal"] = sig

    symbol_spot = sig["symbol"]
    side = sig["side"].upper()
    entry = float(sig["entry"])
    tp1 = float(sig["tp1"])
    tp2 = float(sig["tp2"])
    sl  = float(sig["sl"])
    symbol = to_swap_symbol(symbol_spot)

    ticker = ex.fetch_ticker(symbol)
    last = float(ticker["last"])
    if not within_dev(last, entry, MAX_ENTRY_DEVIATION_PCT):
        await post(f"⚠️ {symbol}: Marktpreis {last:.6f} weicht >{MAX_ENTRY_DEVIATION_PCT:.2f}% von Entry {entry:.6f} ab → **kein Trade**.")
        return

    await set_symbol_params(symbol)
    qty = calc_qty(symbol, entry)
    if qty <= 0:
        await post(f"❌ {symbol}: Menge ≤ 0, abgebrochen.")
        return

    is_long = side == "LONG"
    order_side = "buy" if is_long else "sell"
    reduce_side = "sell" if is_long else "buy"
    cid_base = uuid.uuid4().hex[:12]

    entry_order = ex.create_order(symbol, type="limit", side=order_side, amount=qty, price=entry,
                                  params={"timeInForce": "GTC", "clientOrderId": f"entry-{cid_base}"})
    await post(f"📥 Entry placed {symbol} {side} {qty} @ {entry:.6f}")

    tp1_qty = ex.amount_to_precision(symbol, qty * 0.5)
    tp2_qty = ex.amount_to_precision(symbol, qty - float(tp1_qty))

    tp1_order = ex.create_order(symbol, type="limit", side=reduce_side, amount=tp1_qty, price=tp1,
                                params={"reduceOnly": True, "timeInForce": "GTC", "clientOrderId": f"tp1-{cid_base}"})
    tp2_order = ex.create_order(symbol, type="limit", side=reduce_side, amount=tp2_qty, price=tp2,
                                params={"reduceOnly": True, "timeInForce": "GTC", "clientOrderId": f"tp2-{cid_base}"})
    await post(f"🎯 TP1 {tp1_qty} @ {tp1:.6f} | 🎯 TP2 {tp2_qty} @ {tp2:.6f} (reduceOnly)")

    sl_params = {
        "reduceOnly": True,
        "stopPrice": sl,
        "type": "stop_market",
        "clientOrderId": f"sl-{cid_base}",
        "triggerPrice": sl,
        "positionSide": "LONG" if is_long else "SHORT",
    }
    sl_order = ex.create_order(symbol, type="market", side=reduce_side, amount=qty, params=sl_params)
    await post(f"🛡 SL gesetzt @ {sl:.6f} (volle Menge)")

    status_state["last_order_info"] = {
        "symbol": symbol, "cid_base": cid_base,
        "entry_id": entry_order.get("id"),
        "tp1_id": tp1_order.get("id"),
        "tp2_id": tp2_order.get("id"),
        "sl_id": sl_order.get("id"),
        "entry": entry, "sl": sl,
        "qty": float(qty), "tp1_qty": float(tp1_qty), "tp2_qty": float(tp2_qty),
        "side_long": is_long,
        "be_set": False,
    }
    asyncio.create_task(monitor_and_be_shift(status_state["last_order_info"]))

async def monitor_and_be_shift(info: Dict[str, Any]):
    assert ex is not None
    symbol = info["symbol"]
    be_set = False
    entry_price = info["entry"]
    is_long = info["side_long"]

    while True:
        try:
            o_tp1 = None
            if info.get("tp1_id"):
                try:
                    o_tp1 = ex.fetch_order(info["tp1_id"], symbol)
                except Exception:
                    o_tp1 = None

            tp1_filled = False
            if o_tp1 and o_tp1.get("filled") is not None:
                tp1_filled = float(o_tp1["filled"]) >= float(info["tp1_qty"])

            if tp1_filled and not be_set:
                if info.get("sl_id"):
                    try:
                        ex.cancel_order(info["sl_id"], symbol)
                    except Exception:
                        pass
                rest_qty = ex.amount_to_precision(symbol, float(info["tp2_qty"]))
                reduce_side = "sell" if is_long else "buy"
                be_params = {
                    "reduceOnly": True,
                    "stopPrice": entry_price,
                    "type": "stop_market",
                    "clientOrderId": f"slbe-{info['cid_base']}",
                    "triggerPrice": entry_price,
                    "positionSide": "LONG" if is_long else "SHORT",
                }
                new_sl = ex.create_order(symbol, type="market", side=reduce_side, amount=rest_qty, params=be_params)
                info["sl_id"] = new_sl.get("id")
                info["be_set"] = True
                be_set = True
                if POST_FILLS:
                    await post(f"✅ TP1 erreicht → SL auf **BreakEven** ({entry_price:.6f}) für Restmenge {rest_qty}")

            try:
                positions = ex.fetch_positions([symbol])
                pos = next((p for p in positions if p.get("symbol") == symbol and float(p.get("contracts", 0)) != 0.0), None)
            except Exception:
                pos = None

            if not pos:
                if POST_FILLS:
                    await post(f"ℹ️ Position auf {symbol} ist flat. Monitor beendet.")
                return

            await asyncio.sleep(POLL_INTERVAL_S)
        except Exception as e:
            log.warning("Monitor-Loop err: %s", e)
            await asyncio.sleep(POLL_INTERVAL_S)

# ---------- Parser & Telegram ----------
def parse_signal(text: str) -> Optional[Dict[str, Any]]:
    m = SIG_RE.search(text)
    if not m:
        return None
    gd = m.groupdict()
    try:
        return {
            "symbol": gd["symbol"].upper().strip(),
            "side": gd["side"].upper().strip(),
            "entry": float(gd["entry"]),
            "tp1": float(gd["tp1"]),
            "tp2": float(gd["tp2"]),
            "tp3": float(gd["tp3"]) if gd.get("tp3") else None,
            "sl": float(gd["sl"]),
        }
    except Exception:
        return None

async def on_message(update: Update, context):
    if not update.effective_chat or update.effective_chat.id != SOURCE_CHAT_ID:
        return
    if not update.effective_message or not update.effective_message.text:
        return
    sig = parse_signal(update.effective_message.text)
    if not sig:
        return
    await post(f"📡 Signal empfangen: {sig['symbol']} {sig['side']} | Entry {sig['entry']} | TP1 {sig['tp1']} | TP2 {sig['tp2']} | SL {sig['sl']}")
    await place_trade_from_signal(sig)

# ---------- Startup ----------
async def init_exchange_with_retry():
    global ex
    if not (MEXC_API_KEY and MEXC_API_SECRET):
        raise RuntimeError("MEXC_API_KEY / MEXC_API_SECRET fehlen.")
    tries = 0
    while tries < 5:
        try:
            ex = ccxt.mexc({
                "apiKey": MEXC_API_KEY,
                "secret": MEXC_API_SECRET,
                "enableRateLimit": True,
                "options": {"defaultType": "swap"},
            })
            ex.load_markets()
            status_state["exchange_ok"] = True
            log.info("MEXC connected.")
            return
        except Exception as e:
            tries += 1
            status_state["exchange_ok"] = False
            status_state["err"] = f"Exchange init failed (try {tries}): {e}"
            log.warning(status_state["err"])
            await asyncio.sleep(3)
    raise RuntimeError("Exchange init failed after retries.")

async def start_polling():
    """PTB v20+/v21: run_polling im Hintergrund (nicht blockierend)."""
    global tg_app, tg_bot
    if not TG_TOKEN or not SOURCE_CHAT_ID or not DEST_CHAT_ID:
        raise RuntimeError("TG_TOKEN / SOURCE_CHAT_ID / DEST_CHAT_ID fehlen.")

    tg_app = Application.builder().token(TG_TOKEN).rate_limiter(AIORateLimiter()).build()
    tg_bot = tg_app.bot
    tg_app.add_handler(MessageHandler(filters.ALL, on_message))

    status_state["running"] = True
    # run_polling ist ein Coroutine; als Hintergrund-Task starten:
    asyncio.create_task(
        tg_app.run_polling(
            close_loop=False,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )
    )
    await post("🚀 Executor gestartet (Polling aktiv).")

@app.on_event("startup")
async def _on_start():
    try:
        await init_exchange_with_retry()
        asyncio.create_task(start_polling())
        status_state["ok"] = True
        status_state["err"] = None
    except Exception as e:
        status_state["ok"] = False
        status_state["err"] = str(e)
        log.exception("Startup error: %s", e)

# ---------- API ----------
@app.get("/health")
async def health():
    return {
        "ok": status_state["ok"],
        "running": status_state["running"],
        "exchange_ok": status_state["exchange_ok"],
        "err": status_state["err"],
    }

@app.get("/status")
async def status():
    return status_state
