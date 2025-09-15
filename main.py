import os, asyncio, re, uuid, logging
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
    # hier würde place_trade_from_signal(sig) kommen
    # aktuell nur Test ob Signale ankommen

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
    """Telegram Bot sauber starten"""
    global tg_app, tg_bot
    if not TG_TOKEN or not SOURCE_CHAT_ID or not DEST_CHAT_ID:
        raise RuntimeError("TG_TOKEN / SOURCE_CHAT_ID / DEST_CHAT_ID fehlen.")

    tg_app = Application.builder().token(TG_TOKEN).rate_limiter(AIORateLimiter()).build()
    tg_bot = tg_app.bot
    tg_app.add_handler(MessageHandler(filters.ALL, on_message))

    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(drop_pending_updates=True)

    status_state["running"] = True
    await post("🚀 Executor gestartet (Polling aktiv).")

@app.on_event("startup")
async def _on_start():
    try:
        await init_exchange_with_retry()
        asyncio.create_task(start_polling())
        status_state["ok"] = True
    except Exception as e:
        status_state["ok"] = False
        status_state["err"] = str(e)
        log.exception("Startup error: %s", e)

@app.on_event("shutdown")
async def _on_shutdown():
    if tg_app:
        await tg_app.updater.stop()
        await tg_app.stop()
        await tg_app.shutdown()

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
