ALLOW_SLIPPAGE_PCT   = 0.30             # max 0.30% Abweichung Entry↔Markt
USE_STOP_LOSS        = False            # per Wunsch: kein SL aktiv
STOP_LOSS_PCT        = 0.0              # ignoriert wenn USE_STOP_LOSS=False

# DRY-RUN: True = nur simulieren (keine Orders); per /dryrun_on /dryrun_off umschaltbar
DRY_RUN              = True

# ========= Logging =========
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("mexc-autotrader")

# ========= ccxt MEXC (USDT-Perp) =========
def build_exchange():
    ex = ccxt.mexc({
        "apiKey": MEXC_API_KEY,
        "secret": MEXC_API_SECRET,
        "enableRateLimit": True,
        "options": {
            "defaultType": "swap",  # USDT-M Perp
        },
    })
    return ex

ex = build_exchange()
_seen_message_ids = set()

# ========= Helpers =========
def to_perp(symbol_txt: str) -> str:
    """
    Map "STX/USDT" -> "STX/USDT:USDT" (MEXC USDT-Perp Symbolformat in ccxt)
    """
    base, quote = symbol_txt.upper().split("/")
    return f"{base}/USDT:USDT"

def round_amount(exchange: ccxt.Exchange, symbol: str, amount: float) -> float:
    return float(exchange.amount_to_precision(symbol, amount))

def within_slippage(entry: float, last: float, max_pct: float) -> bool:
    if entry <= 0 or last <= 0: return False
    diff = abs(last - entry) / entry * 100.0
    return diff <= max_pct

def side_and_reduce(direction: str) -> Tuple[str, str]:
    """
    Returns (entry_side, reduce_side)
    LONG -> buy entry; TPs are sell
    SHORT -> sell entry; TPs are buy
    """
    d = direction.upper()
    return ("buy", "sell") if d == "LONG" else ("sell", "buy")

# ========= Signal-Parser (robust für deine STRIKT-Message) =========
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

# ========= Telegram Utils =========
async def dm_owner(context: ContextTypes.DEFAULT_TYPE, text: str):
    try:
        await context.bot.send_message(chat_id=OWNER_USER_ID, text=text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

async def reply(update: Update, text: str):
    try:
        await update.effective_chat.send_message(text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

# ========= Trading Core =========
def set_isolated_and_leverage(exchange: ccxt.Exchange, symbol: str, lev: int):
    """
    Best effort: Isolated + Leverage setzen (je nach ccxt-Version/MEXC-Support).
    """
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
    """
    Führt Entry (Market) aus & legt TPs an (reduce-only Limit).
    Nutzt 50 USDT Margin * 20x / Entry = Menge.
    """
    entry_side, reduce_side = side_and_reduce(direction)

    # Positionsgröße
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty_raw = notion_usdt / max(entry, 1e-9)
    qty = round_amount(exchange, symbol, qty_raw)
    if qty <= 0:
        raise ExchangeError("Menge zu klein nach Rundung – Symbol-Precision prüfen.")

    # Slippage-Check ggü. Marktpreis
    last = get_last_price(exchange, symbol)
    if not within_slippage(entry, last, ALLOW_SLIPPAGE_PCT):
        raise ExchangeError(f"Preisabweichung zu groß: entry {entry} vs last {last} (>{ALLOW_SLIPPAGE_PCT}%)")

    # Margin-Modus & Leverage
    set_isolated_and_leverage(exchange, symbol, LEVERAGE)

    # Entry: Market
    entry_order = exchange.create_order(symbol, "market", entry_side, qty, None, {"reduceOnly": False})

    # TPs (reduce-only Limits)
    def tp_qty(frac: float) -> float:
        return round_amount(exchange, symbol, qty * frac)

    tp_orders = []
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.20), tp1, {"reduceOnly": True}))
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(0.50), tp2, {"reduceOnly": True}))
    rest_share = max(0.0, 1.0 - 0.20 - 0.50)
    last_tp_price = tp3 if tp3 is not None else tp2
    tp_orders.append(exchange.create_order(symbol, "limit", reduce_side, tp_qty(rest_share), last_tp_price, {"reduceOnly": True}))

    # Optional: SL (deaktiviert)
    sl_order = None
    if USE_STOP_LOSS and STOP_LOSS_PCT > 0:
        if direction.upper() == "LONG":
            sl_price = round(entry * (1.0 - STOP_LOSS_PCT / 100.0), 8)
            sl_order = exchange.create_order(symbol, "stop_market", "sell", qty, None,
                                             {"stopPrice": sl_price, "reduceOnly": True})
        else:
            sl_price = round(entry * (1.0 + STOP_LOSS_PCT / 100.0), 8)
            sl_order = exchange.create_order(symbol, "stop_market", "buy", qty, None,
                                             {"stopPrice": sl_price, "reduceOnly": True})

    return {
        "entry_order": entry_order,
        "tp_orders": tp_orders,
        "sl_order": sl_order,
        "qty": qty,
        "last": last,
    }

def simulate_entry_and_tps(symbol: str, direction: str, entry: float, tp1: float, tp2: float, tp3: Optional[float]) -> dict:
    """
    DRY-RUN: keine echten Orders; gibt berechnete Größen zurück.
    """
    notion_usdt = MARGIN_USDT * LEVERAGE
    qty = notion_usdt / max(entry, 1e-9)
    return {
        "entry_side": "buy" if direction.upper()=="LONG" else "sell",
        "reduce_side": "sell" if direction.upper()=="LONG" else "buy",
        "qty": qty,
        "tp_qtys": {
            "tp1": qty*0.20,
            "tp2": qty*0.50,
            "tp3": qty*(1.0-0.70),
        },
        "tp_prices": {
            "tp1": tp1,
            "tp2": tp2,
            "tp3": tp3 if tp3 is not None else tp2,
        },
    }

# ========= Message Handler =========
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg is None:
        return

    # Nur den gewünschten Kanal verarbeiten (und ggf. deine DMs für /test)
    if msg.chat_id not in (TELEGRAM_CHANNEL_ID, OWNER_USER_ID):
        return

    # Doppelte Nachrichten vermeiden
    if msg.message_id in _seen_message_ids:
        return
    _seen_message_ids.add(msg.message_id)

    text = (msg.text or msg.caption or "").strip()
    m = SIGNAL_RE.search(text)
    if not m:
        return  # keine passende Signal-Message

    symbol_txt = m.group("symbol").upper()         # z.B. STX/USDT
    direction  = m.group("side").upper()           # LONG/SHORT
    entry      = float(m.group("entry"))
    tp1        = float(m.group("tp1"))
    tp2        = float(m.group("tp2"))
    tp3        = float(m.group("tp3")) if m.group("tp3") else None

    perp = to_perp(symbol_txt)                     # STX/USDT:USDT

    try:
        await asyncio.to_thread(ex.load_markets)
        if perp not in ex.markets:
            raise ExchangeError(f"Symbol nicht gefunden: {perp}")

        if DRY_RUN:
            sim = simulate_entry_and_tps(perp, direction, entry, tp1, tp2, tp3)
            conf = (
                f"🧪 <b>DRY-RUN</b> erkannt und simuliert\n"
                f"• Symbol: <code>{perp}</code>\n"
                f"• Seite: <b>{direction}</b>\n"
                f"• Entry(ref): <code>{entry}</code>\n"
                f"• Menge: <code>{sim['qty']:.6f}</code> (50 USDT x 20 / Entry)\n"
                f"• TP1/TP2/TP3: <code>{tp1}</code> / <code>{tp2}</code> / <code>{tp3 if tp3 else tp2}</code>\n"
                f"• Verteilung: 20% / 50% / 30%\n"
                f"• Hinweis: Mit /dryrun_off echte Orders aktivieren."
            )
            await msg.reply_html(conf)
            await dm_owner(context, conf)
            return

        # ECHTER HANDEL
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
            f"• Margin: <code>{MARGIN_USDT} USDT</code> @ {LEVERAGE}x"
        )
        await msg.reply_html(conf)
        await dm_owner(context, conf)

    except Exception as e:
        err = f"❌ Trade-Fehler: <code>{type(e).__name__}</code> — {e}"
        log.exception(err)
        await msg.reply_html(err)
        await dm_owner(context, err)

# ========= Commands (Tests & Steuerung) =========
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
        "  /testsignal – Beispiel-Signal posten (Parsing & ggf. Order)\n"
        "  /ticker STX/USDT – Marktticker checken (Perp)\n"
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
    await reply(update, "🧪 DRY-RUN ist jetzt <b>ON</b> (keine echten Orders).")

async def cmd_dryrun_off(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global DRY_RUN
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    DRY_RUN = False
    await reply(update, "🚀 DRY-RUN ist jetzt <b>OFF</b> (ECHTE Orders aktiv).")

async def cmd_testsignal(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user and update.effective_user.id != OWNER_USER_ID:
        return
    # Beispiel-Signal wie deine Bot-Messages
    sample = (
        "🛡 STRIKT — STX/USDT 5m\n"
        "➡️ *SHORT*\n"
        "🎯 Entry: `0.643`\n"
        "🏁 TP1: `0.641297`\n"
        "🏁 TP2: `0.639253`\n"
        "🏁 TP3: `0.637209`\n"
    )
    fake_update = Update(
        update.update_id,
        message=update.effective_message  # wir verwenden die gleiche Message zum Antworten
    )
    # Direkt den Handler aufrufen (Parser & ggf. Trade)
    await on_message(update, context)
    await reply(update, "🧪 Testsignal gesendet.\n(Um echten Trade zu machen: /dryrun_off und dann dein Real-Signal im Kanal posten.)")

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

# ========= Bootstrap =========
async def on_startup(app: Application):
    try:
        await asyncio.to_thread(ex.load_markets)
    except Exception as e:
        log.warning(f"load_markets warn: {e}")
    try:
        await app.bot.send_message(chat_id=OWNER_USER_ID, text="🤖 MEXC-Autotrader gestartet.", parse_mode=ParseMode.HTML)
    except Exception:
        pass

def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("ping", cmd_ping))
    app.add_handler(CommandHandler("dryrun_on", cmd_dryrun_on))
    app.add_handler(CommandHandler("dryrun_off", cmd_dryrun_off))
    app.add_handler(CommandHandler("testsignal", cmd_testsignal))
    app.add_handler(CommandHandler("ticker", cmd_ticker))
    # Alle Kanalposts/Signale durchs on_message
    app.add_handler(MessageHandler(filters.ALL, on_message))
    app.post_init(on_startup)
    log.info("Bot polling …")
    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
