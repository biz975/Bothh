# server.py
import threading
from fastapi import FastAPI

# dein bestehendes Bot-Programm
import main as bot

def run_bot():
    # blockiert normalerweise – daher im Thread
    bot.main()

# Bot in Hintergrund-Thread starten
t = threading.Thread(target=run_bot, daemon=True)
t.start()

# Minimaler HTTP-Server für Render
app = FastAPI()

@app.get("/")
def root():
    return {"ok": True, "service": "telegram-bot", "status": "running"}
