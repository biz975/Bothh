# -*- coding: utf-8 -*-
"""
MEXC Auto-Trader (STRIKT Signale über Telegram)
- Liest Signale aus deinem Kanal
- 50 USDT Margin, 20x Leverage (per ENV konfigurierbar)
- TP1 20%, TP2 50%, TP3 Rest
- Nach TP1: SL -> BreakEven
- DRY_RUN = True (nur Simulation) bis man es abschaltet
"""

import os
import re
import math
import asyncio
import logging
from typing import Optional, Tuple

import ccxt
from ccxt.base.errors import ExchangeError
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ========= ENV-Variablen =========
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "0"))
OWNER_USER_ID       = int(os.getenv("OWNER_USER_ID", "0"))

MEXC_API_KEY        = os.getenv("MEXC_API_KEY")
MEXC_API_SECRET     = os.getenv("MEXC_API_SECRET")

MARGIN_USDT         = float(os.getenv("MARGIN_USDT", "50"))
LEVERAGE            = int(os.getenv("LEVERAGE", "20"))
ALLOW_SLIPPAGE_PCT  = float(os.getenv("ALLOW_SLIPPAGE_PCT", "0.30"))
DRY_RUN             = os.getenv("DRY_RUN", "True").lower() == "true"

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mexc-autotrader")

# ========= ccxt MEXC =========
def build_exchange():
    return ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })

ex = build_exchange()
_seen_message_ids = set()

# ========= Helpers =========
def to_perp(symbol_txt: str) -> str:
    base, _ = symbol_txt.upper().split("/")
    return f"{base}/USDT:USDT"

def round_amount(exchange: ccxt.Exchange, symbol: str, amount: float) -> float:
    return float(exchange.amount_to_precision(symbol, amount))

def within_slippage(entry: float, last: float, max_pct: float) -> bool:
    diff = abs(last - entry) / entry * 100.0
    return diff <= max_pct

def side_and_reduce(direction: str) -> Tuple[str, str]:
    return ("buy", "sell") if direction.upper() == "LONG" else ("sell", "buy")

# ========= Signal Parser =========
SIGNAL_RE = re.compile(
    r"""(?ix)
    ^.*?STRIKT.*?
    (?P<symbol>[A-Z0-9]+\/USDT).*?\n
    .*?➡️\s*\*(?P<side>LONG|SHORT)\*.*?\n
    .*?Entry:\s*`?(?P<entry>\d+(\.\d+)?)`?.*?\n
    .*?TP1:\s*`?(?P<tp1>\d+(\.\d+)?)`?.*?\n
    .*?TP2:\s*`?(?P<tp2>\d+(\.\d+)?)`?.*?\n
    (?:.*?TP3:\s*`?(?P<tp3>\d+(\.\d+)?)`?.*?\n)?
    """, re.DOTALL
)

# ========= Telegram Utils =========
async def dm_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=OWNER_USER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ========= Trading Core =========
def set_isolated_and_leverage(exchange: ccxt.Exchange, symbol: str, lev: int):
    try:
        exchange.set_margin_mode("isolated", symbol, {"leverage": lev})
    except Exception:
        try: exchange.set_margin_mode("isolated", symbol)
        except Exception: pass
        try: exchange.set_leverage(lev, symbol)
        except Exception: pass

def get_last_price(exchange: ccxt.Exchange, symbol: str) -> float:
    t = exchange.fetch_ticker(symbol)
    return float(t["last"])

def place_entry_and_tps(exchange: ccxt.Exchange, symbol: str, direction: str,
                        entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> dict:
    entry_side, reduce_side = side_and_reduce(direction)
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty = round_amount(exchange, symbol, notion_usdt / entry)
    last = get_last_price(exchange, symbol)

    if not within_slippage(entry, last, ALLOW_SLIPPAGE_PCT):
        raise ExchangeError("Preisabweichung zu groß")

    set_isolated_and_leverage(exchange, symbol, LEVERAGE)

    entry_order = exchange.create_order(symbol, "market", entry_side, qty, None, {"reduceOnly": False})

    def tp_qty(frac: float) -> float:
        return round_amount(exchange, symbol, qty * frac)

    tp_orders = []
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.20), tp1, {"reduceOnly": True}))
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.50), tp2, {"reduceOnly": True}))
    rest_share = 0.30
    last_tp_price = tp3 if tp3 else tp2
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(rest_share), last_tp_price, {"reduceOnly": True}))

    # SL auf BreakEven nach TP1 (OCO-ähnlich, einfach als stop_market)
    sl_order = exchange.create_order(
        symbol, "stop_market", reduce_side, qty, None,
        {"stopPrice": entry, "reduceOnly": True}
    )

    return {"entry_order": entry_order, "tp_orders": tp_orders, "sl_order": sl_order, "qty": qty, "last": last}

def simulate_entry(symbol: str, direction: str, entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> dict:
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty = notion_usdt / entry
    return {
        "side": direction,
        "qty": qty,
        "tp_prices": {"tp1": tp1, "tp2": tp2, "tp3": tp3 if tp3 else tp2}
    }

# ========= Message Handler =========
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id not in (TELEGRAM_CHANNEL_ID, OWNER_USER_ID): return
    if msg.message_id in _seen_message_ids: return
    _seen_message_ids.add(msg.message_id)

    text = (msg.text or msg.caption or "").strip()
    m = SIGNAL_RE.search(text)
    if not m: return

    symbol_txt, direction = m.group("symbol").upper(), m.group("side").upper()
    entry, tp1, tp2 = float(m.group("entry")), float(m.group("tp1")), float(m.group("tp2"))
    tp3 = float(m.group("tp3")) if m.group("tp3") else None
    perp = to_perp(symbol_txt)

    try:
        await asyncio.to_thread(ex.load_markets)
        if perp not in ex.markets: raise ExchangeError(f"Symbol nicht gefunden: {perp}")

        if DRY_RUN:
            sim = simulate_entry(perp, direction, entry, tp1, tp2, tp3)
            conf = (f"🧪 DRY-RUN\nSymbol: {perp}\nSide: {direction}\nQty: {sim['qty']:.4f}\n"
                    f"TP1/2/3: {tp1} / {tp2} / {tp3 if tp3 else tp2}\nVerteilung: 20/50/30")
            await msg.reply_html(conf); await dm_owner(context, conf); return

        result = await asyncio.to_thread(place_entry_and_tps, ex, perp, direction, entry, tp1, tp2, tp3)
        conf = (f"✅ Trade ausgeführt\nSymbol: {perp}\nSide: {direction}\nEntry: {entry} (Markt {result['last']})\n"
                f"Qty: {result['qty']}\nTP1/2/3: {tp1} / {tp2} / {tp3 if tp3 else tp2}")
        await msg.reply_html(conf); await dm_owner(context, conf)

    except Exception as e:
        err = f"❌ Fehler: {type(e).__name__} — {e}"
        await msg.reply_html(err); await dm_owner(context, err)

# ========= Commands =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_USER_ID: return
    text = f"🤖 Autotrader aktiv.\nDRY_RUN: {DRY_RUN}\nMargin: {MARGIN_USDT} USDT @ {LEVERAGE}x"
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)

# ========= Bootstrap =========
async def on_startup(app: Application):
    try: await asyncio.to_thread(ex.load_markets)
    except Exception as e: log.warning(f"load_markets warn: {e}")
    try: await app.bot.send_message(chat_id=OWNER_USER_ID, text="🤖 Autotrader gestartet.", parse_mode=ParseMode.HTML)
    except Exception: pass

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(filters.ALL, on_message))
    log.info("Bot polling …")
    app.run_polling()

if __name__ == "__main__":
    main()
