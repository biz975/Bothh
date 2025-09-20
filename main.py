# -*- coding: utf-8 -*-
"""
Render Web Service + Telegram Worker
- Startet FastAPI (für Render Port/Health)
- Startet den Telegram-Bot als Background-Task beim Server-Start
"""

import os
import re
import math
import asyncio
import logging
from typing import Optional, Tuple

import ccxt
from ccxt.base.errors import ExchangeError

from fastapi import FastAPI
from contextlib import asynccontextmanager

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, MessageHandler, CommandHandler, filters, ContextTypes

# ========= ENV (Render Dashboard -> Environment Variables) =========
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL_ID = int(os.getenv("TELEGRAM_CHANNEL_ID", "0"))
OWNER_USER_ID       = int(os.getenv("OWNER_USER_ID", "0"))
MEXC_API_KEY        = os.getenv("MEXC_API_KEY", "")
MEXC_API_SECRET     = os.getenv("MEXC_API_SECRET", "")

DRY_RUN             = os.getenv("DRY_RUN", "True").lower() == "true"
MARGIN_USDT         = float(os.getenv("MARGIN_USDT", "50"))
LEVERAGE            = int(os.getenv("LEVERAGE", "20"))
ALLOW_SLIPPAGE_PCT  = float(os.getenv("ALLOW_SLIPPAGE_PCT", "0.30"))

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mexc-autotrader")

# ========= ccxt (USDT-Perp) =========
def build_exchange():
    ex = ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    return ex

ex = build_exchange()
_seen_message_ids = set()

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

SIGNAL_RE = re.compile(
    r"""(?ix)
    ^.*?STRIKT.*?
    (?P<symbol>[A-Z0-9]+\/USDT).*?\n
    .*?➡️\s*\*(?P<side>LONG|SHORT)\*.*?\n
    .*?Entry:\s*`?(?P<entry>\d+(\.\d+)?)`?.*?\n
    .*?TP1:\s*`?(?P<tp1>\d+(\.\d+)?)`?.*?\n
    .*?TP2:\s*`?(?P<tp2>\d+(\.\d+)?)`?.*?\n
    (?:.*?TP3:\s*`?(?P<tp3>\d+(\.\d+)?)`?.*?\n)?
    """,
    re.DOTALL
)

async def dm_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        if OWNER_USER_ID:
            await context.bot.send_message(chat_id=OWNER_USER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def reply(update: Update, text: str):
    try:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

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

def place_entry_and_tps(exchange: ccxt.Exchange, symbol: str, direction: str,
                        entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> dict:
    entry_side, reduce_side = side_and_reduce(direction)
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty_raw = notion_usdt / max(entry, 1e-9)
    qty = round_amount(exchange, symbol, qty_raw)
    if qty <= 0:
        raise ExchangeError("Menge zu klein nach Rundung.")

    last = get_last_price(exchange, symbol)
    if not within_slippage(entry, last, ALLOW_SLIPPAGE_PCT):
        raise ExchangeError(f"Preisabweichung zu groß: entry {entry} vs last {last} (>{ALLOW_SLIPPAGE_PCT}%)")

    set_isolated_and_leverage(exchange, symbol, LEVERAGE)

    entry_order = exchange.create_order(symbol, "market", entry_side, qty, None, {"reduceOnly": False})

    def tp_qty(frac: float) -> float:
        return round_amount(exchange, symbol, qty * frac)

    tp_orders = []
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.20), tp1, {"reduceOnly": True}))
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.50), tp2, {"reduceOnly": True}))
    rest_share = max(0.0, 1.0 - 0.20 - 0.50)
    last_tp_price = tp3 if tp3 is not None else tp2
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(rest_share), last_tp_price, {"reduceOnly": True}))

    return {"entry_order": entry_order, "tp_orders": tp_orders, "qty": qty, "last": last}

def simulate_entry_and_tps(symbol: str, direction: str, entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> dict:
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty = notion_usdt / max(entry, 1e-9)
    return {
        "entry_side": "buy" if direction.upper()=="LONG" else "sell",
        "reduce_side": "sell" if direction.upper()=="LONG" else "buy",
        "qty": qty,
        "tp_qtys": {"tp1": qty*0.20, "tp2": qty*0.50, "tp3": qty*(1.0-0.70)},
        "tp_prices": {"tp1": tp1, "tp2": tp2, "tp3": tp3 if tp3 is not None else tp2},
    }

# ========== Telegram BOT ==========
bot_app: Optional[Application] = None  # global, damit wir sie in FastAPI-Startup benutzen

async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return
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
                f"• Entry: <code>{entry}</code>\n"
                f"• Menge: <code>{sim['qty']:.6f}</code>\n"
                f"• TP1/2/3: <code>{tp1}</code> / <code>{tp2}</code> / <code>{tp3 if tp3 else tp2}</code>\n"
                f"• Verteilung: 20% / 50% / 30%"
            )
            await msg.reply_html(conf)
            await dm_owner(context, conf)
            return

        result = await asyncio.to_thread(place_entry_and_tps, ex, perp, direction, entry, tp1, tp2, tp3)
        conf = (
            f"✅ <b>Trade ausgeführt</b>\n"
            f"• Symbol: <code>{perp}</code>\n"
            f"• Seite: <b>{direction}</b>\n"
            f"• Entry(ref): <code>{entry}</code> | Markt: <code>{result['last']}</code>\n"
            f"• Menge: <code>{result['qty']}</code>\n"
            f"• TP1/2/3: <code>{tp1}</code> / <code>{tp2}</code> / <code>{tp3 if tp3 else tp2}</code>\n"
            f"• Margin: <code>{MARGIN_USDT} USDT</code> @ {LEVERAGE}x"
        )
        await msg.reply_html(conf)
        await dm_owner(context, conf)

    except Exception as e:
        err = f"❌ Trade-Fehler: <code>{type(e).__name__}</code> — {e}"
        log.exception(err)
        await msg.reply_html(err)
        await dm_owner(context, err)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    text = (
        "🤖 Autotrader läuft als Web Service (Render).\n"
        f"• DRY-RUN: {'ON' if DRY_RUN else 'OFF'}\n"
        "Kommandos: /ping, /dryrun_on, /dryrun_off, /ticker STX/USDT"
    )
    await reply(update, text)

async def cmd_ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    try:
        await asyncio.to_thread(ex.load_markets)
        t = await asyncio.to_thread(ex.fetch_ticker, "BTC/USDT:USDT")
        await reply(update, f"🏓 Pong! BTC Perp last: <b>{t['last']}</b>")
    except Exception as e:
        await reply(update, f"❌ {e}")

async def cmd_dryrun_on(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRY_RUN
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    DRY_RUN = True
    await reply(update, "🧪 DRY-RUN ist jetzt <b>ON</b>.")

async def cmd_dryrun_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRY_RUN
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    DRY_RUN = False
    await reply(update, "🚀 DRY-RUN ist jetzt <b>OFF</b> (echte Orders).")

async def cmd_ticker(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    try:
        if not context.args:
            await reply(update, "Nutze: <code>/ticker STX/USDT</code>")
            return
        spot = context.args[0].upper()
        perp = to_perp(spot)
        await asyncio.to_thread(ex.load_markets)
        t = await asyncio.to_thread(ex.fetch_ticker, perp)
        await reply(update, f"📈 {perp} last: <b>{t['last']}</b>  bid: {t['bid']}  ask: {t['ask']}")
    except Exception as e:
        await reply(update, f"❌ {e}")

def build_bot_app() -> Application:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("dryrun_on", cmd_dryrun_on))
    app.add_handler(CommandHandler("dryrun_off", cmd_dryrun_off))
    app.add_handler(CommandHandler("ticker", cmd_ticker))
    app.add_handler(MessageHandler(filters.ALL, on_message))
    return app

# ========== FastAPI + Lifespan: Bot als Background-Task ==========
@asynccontextmanager
async def lifespan(app: FastAPI):
    global bot_app
    bot_app = build_bot_app()
    # Bot im Hintergrund starten
    bot_task = asyncio.create_task(bot_app.run_polling(close_loop=False))
    log.info("Telegram bot started (background).")
    try:
        yield
    finally:
        try:
            await bot_app.shutdown()
        except Exception:
            pass
        bot_task.cancel()
        log.info("Telegram bot stopped.")

app = FastAPI(title="Bothh Web+Worker", lifespan=lifespan)

@app.get("/")
async def root():
    return {"ok": True, "service": "web+worker", "dry_run": DRY_RUN}

@app.get("/health")
async def health():
    return {"ok": True}
