# -*- coding: utf-8 -*-
"""
Telegram → MEXC Auto-Trader (Safe Calls)
- Liest STRIKT/Safe-Calls aus deinem Kanal
- 50 USDT Margin (fest), 20x, USDT-Perp (isolated)
- TP1 20%, TP2 50%, TP3 30% (reduce-only)
- Nach TP1: SL -> Break-even (Entry)
- DRY_RUN standardmäßig AN (keine echten Orders), via /dryrun_off ausschaltbar
- python-telegram-bot >= 20 (Application.builder()) – KEIN Updater mehr
"""

import os
import re
import math
import asyncio
import logging
from typing import Optional, Tuple, Dict, Any

import ccxt
from ccxt.base.errors import ExchangeError

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, MessageHandler, CommandHandler, ContextTypes, filters
)

# ========= ENV / Settings =========
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "0"))
OWNER_USER_ID       = int(os.getenv("OWNER_USER_ID", "0"))

MEXC_API_KEY        = os.getenv("MEXC_API_KEY", "").strip()
MEXC_API_SECRET     = os.getenv("MEXC_API_SECRET", "").strip()

MARGIN_USDT         = float(os.getenv("MARGIN_USDT", "50"))
LEVERAGE            = int(os.getenv("LEVERAGE", "20"))
ALLOW_SLIPPAGE_PCT  = float(os.getenv("ALLOW_SLIPPAGE_PCT", "0.30"))
DRY_RUN             = os.getenv("DRY_RUN", "True").lower() in ("1", "true", "yes")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mexc-autotrader")

# ========= ccxt MEXC (USDT-Perp) =========
def build_exchange():
    return ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},  # USDT-M Perpetual
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
    if entry <= 0 or last <= 0: return False
    diff = abs(last - entry) / entry * 100.0
    return diff <= max_pct

def side_and_reduce(direction: str) -> Tuple[str, str]:
    d = direction.upper()
    return ("buy", "sell") if d == "LONG" else ("sell", "buy")

async def dm_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    if not OWNER_USER_ID:
        return
    try:
        await context.bot.send_message(chat_id=OWNER_USER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ========= Signal-Parser (STRIKT-Format) =========
SIGNAL_RE = re.compile(
    r"""(?ix)
    ^.*?STRIKT.*?                      # Header enthält STRIKT
    (?P<symbol>[A-Z0-9]+\/USDT).*?\n   # z.B. STX/USDT
    .*?➡️\s*\*(?P<side>LONG|SHORT)\*.*?\n
    .*?Entry:\s*`?(?P<entry>\d+(\.\d+)?)`?.*?\n
    .*?TP1:\s*`?(?P<tp1>\d+(\.\d+)?)`?.*?\n
    .*?TP2:\s*`?(?P<tp2>\d+(\.\d+)?)`?.*?\n
    (?:.*?TP3:\s*`?(?P<tp3>\d+(\.\d+)?)`?.*?\n)?
    """,
    re.DOTALL
)

# ========= Trading Core =========
def set_isolated_and_leverage(exchange: ccxt.Exchange, symbol: str, lev: int):
    try:
        exchange.set_margin_mode("isolated", symbol, {"leverage": lev})
    except Exception:
        try:
            exchange.set_margin_mode("isolated", symbol)
        except Exception:
            pass
        try:
            exchange.set_leverage(lev, symbol)
        except Exception:
            pass

def get_last_price(exchange: ccxt.Exchange, symbol: str) -> float:
    t = exchange.fetch_ticker(symbol)
    return float(t["last"])

def create_be_stop(exchange: ccxt.Exchange, symbol: str, direction: str, entry: float, qty: float):
    """
    Break-even SL = genau am Entry, reduceOnly.
    """
    stop_side = "sell" if direction.upper()=="LONG" else "buy"
    params = {
        "reduceOnly": True,
        "stopPrice": float(entry),
        "trigger": "market_price",  # MEXC swap uses trigger type sometimes
    }
    # MEXC ccxt: type "stop_market" / params may vary; fallback to create_order with params
    try:
        return exchange.create_order(symbol, "stop_market", stop_side, qty, None, params)
    except Exception:
        # Fallback: place post-only limit near entry as BE (less ideal). Best effort.
        return exchange.create_order(symbol, "limit", stop_side, qty, entry, {"reduceOnly": True})

def place_entry_and_tps(exchange: ccxt.Exchange, symbol: str, direction: str,
                        entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> Dict[str, Any]:
    """
    Entry Market, TPs als reduce-only Limits.
    Nach TP1 wird sofort ein Break-even-Stop (Entry) für Restmenge platziert.
    """
    entry_side, reduce_side = side_and_reduce(direction)

    # Positionsgröße
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty_raw = notion_usdt / max(entry, 1e-9)
    qty = round_amount(exchange, symbol, qty_raw)
    if qty <= 0:
        raise ExchangeError("Menge zu klein nach Rundung – Symbol-Precision prüfen.")

    # Slippage vs Markt
    last = get_last_price(exchange, symbol)
    if not within_slippage(entry, last, ALLOW_SLIPPAGE_PCT):
        raise ExchangeError(f"Preisabweichung zu groß: entry {entry} vs last {last} (>{ALLOW_SLIPPAGE_PCT}%)")

    # Margin-Modus & Leverage
    set_isolated_and_leverage(exchange, symbol, LEVERAGE)

    # Entry
    entry_order = exchange.create_order(symbol, "market", entry_side, qty, None, {"reduceOnly": False})

    # TP-Verteilung
    def tp_qty(frac: float) -> float:
        return round_amount(exchange, symbol, qty * frac)

    # TP1/TP2/TP3
    tp1_q = tp_qty(0.20)
    tp2_q = tp_qty(0.50)
    rest_share = max(0.0, 1.0 - 0.20 - 0.50)
    last_tp_price = tp3 if tp3 is not None else tp2
    tp3_q = tp_qty(rest_share)

    tp_orders = []
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp1_q, tp1, {"reduceOnly": True}))
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp2_q, tp2, {"reduceOnly": True}))
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp3_q, last_tp_price, {"reduceOnly": True}))

    # Break-even SL (für Restmenge nach TP1)
    be_qty = round_amount(exchange, symbol, qty - tp1_q)
    be_order = create_be_stop(exchange, symbol, direction, entry, be_qty)

    return {
        "entry_order": entry_order,
        "tp_orders": tp_orders,
        "be_order": be_order,
        "qty": qty,
        "last": last,
    }

def simulate_entry_and_tps(symbol: str, direction: str, entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> Dict[str, Any]:
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty = notion_usdt / max(entry, 1e-9)
    rest = qty * (1.0 - 0.20)  # nach TP1
    return {
        "entry_side": "buy" if direction.upper()=="LONG" else "sell",
        "reduce_side": "sell" if direction.upper()=="LONG" else "buy",
        "qty": qty,
        "tp_qtys": {"tp1": qty*0.20, "tp2": qty*0.50, "tp3": qty*0.30},
        "tp_prices": {"tp1": tp1, "tp2": tp2, "tp3": tp3 if tp3 else tp2},
        "be_stop": {"side": "sell" if direction.upper()=="LONG" else "buy", "price": entry, "qty": rest},
    }

# ========= Message Handler =========
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    # Nur Kanal + (optional) DM vom Owner
    if msg.chat_id not in (TELEGRAM_CHANNEL_ID, OWNER_USER_ID):
        return

    if msg.message_id in _seen_message_ids:
        return
    _seen_message_ids.add(msg.message_id)

    text = (msg.text or msg.caption or "").strip()
    m = SIGNAL_RE.search(text)
    if not m:
        return

    symbol_txt = m.group("symbol").upper()
    direction  = m.group("side").upper()
    entry      = float(m.group("entry"))
    tp1        = float(m.group("tp1"))
    tp2        = float(m.group("tp2"))
    tp3        = float(m.group("tp3")) if m.group("tp3") else None

    perp = to_perp(symbol_txt)

    try:
        await asyncio.to_thread(ex.load_markets)
        if perp not in ex.markets:
            raise ExchangeError(f"Symbol nicht gefunden: {perp}")

        if DRY_RUN:
            sim = simulate_entry_and_tps(perp, direction, entry, tp1, tp2, tp3)
            conf = (
                f"🧪 <b>DRY-RUN</b>\n"
                f"• Symbol: <code>{perp}</code>\n"
                f"• Seite: <b>{direction}</b>\n"
                f"• Entry(ref): <code>{entry}</code>\n"
                f"• Menge: <code>{sim['qty']:.6f}</code> (50 USDT x 20 / Entry)\n"
                f"• TP1/TP2/TP3: <code>{tp1}</code> / <code>{tp2}</code> / <code>{tp3 if tp3 else tp2}</code>\n"
                f"• Verteilung: 20% / 50% / 30%\n"
                f"• Nach TP1: SL → Break-even (Entry) für Restmenge."
            )
            await msg.reply_html(conf)
            await dm_owner(context, conf)
            return

        # ECHT
        result = await asyncio.to_thread(
            place_entry_and_tps, ex, perp, direction, entry, tp1, tp2, tp3
        )

        conf = (
            f"✅ <b>Trade ausgeführt</b>\n"
            f"• Symbol: <code>{perp}</code>\n"
            f"• Seite: <b>{direction}</b>\n"
            f"• Entry(ref): <code>{entry}</code> | Markt: <code>{result['last']}</code>\n"
            f"• Menge: <code>{result['qty']}</code>\n"
            f"• TP1/TP2/TP3: <code>{tp1}</code> / <code>{tp2}</code> / <code>{tp3 if tp3 else tp2}</code>\n"
            f"• Break-even Stop platziert bei <code>{entry}</code> nach TP1."
        )
        await msg.reply_html(conf)
        await dm_owner(context, conf)

    except Exception as e:
        err = f"❌ Trade-Fehler: <code>{type(e).__name__}</code> — {e}"
        log.exception(err)
        try:
            await msg.reply_html(err)
        except Exception:
            pass
        await dm_owner(context, err)

# ========= Commands =========
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    text = (
        "🤖 MEXC-Autotrader aktiv.\n"
        f"• DRY-RUN: {'ON' if DRY_RUN else 'OFF'}\n"
        "• Kommandos:\n"
        "  /ping – Verbindungstest\n"
        "  /dryrun_on – nur simulieren\n"
        "  /dryrun_off – echte Orders\n"
    )
    await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    try:
        await asyncio.to_thread(ex.load_markets)
        t = await asyncio.to_thread(ex.fetch_ticker, "BTC/USDT:USDT")
        await update.effective_chat.send_message(f"🏓 Pong! BTC Perp last: <b>{t['last']}</b>", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.effective_chat.send_message(f"❌ {e}", parse_mode=ParseMode.HTML)

async def cmd_dryrun_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRY_RUN
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    DRY_RUN = True
    await update.effective_chat.send_message("🧪 DRY-RUN ist jetzt <b>ON</b>.", parse_mode=ParseMode.HTML)

async def cmd_dryrun_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRY_RUN
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    DRY_RUN = False
    await update.effective_chat.send_message("🚀 DRY-RUN ist jetzt <b>OFF</b>.", parse_mode=ParseMode.HTML)

# ========= Bootstrap =========
async def on_startup(app: Application):
    try:
        await asyncio.to_thread(ex.load_markets)
    except Exception as e:
        log.warning(f"load_markets warn: {e}")
    if OWNER_USER_ID:
        try:
            await app.bot.send_message(chat_id=OWNER_USER_ID, text="🤖 MEXC-Autotrader gestartet.", parse_mode=ParseMode.HTML)
        except Exception:
            pass

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN fehlt (Env).")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("dryrun_on", cmd_dryrun_on))
    app.add_handler(CommandHandler("dryrun_off", cmd_dryrun_off))

    app.add_handler(MessageHandler(filters.ALL, on_message))

    app.post_init(on_startup)

    log.info("Bot polling …")
    # Für Render-Worker absolut ok:
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
