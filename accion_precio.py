"""
PALMERO - Accion del Precio (VERSIÓN SIMPLIFICADA)
Sin cache complicado. Datos frescos de Binance cada vez.
"""

import os
import requests
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)

# AUTENTICACION
API_KEY = os.environ.get("PALMERO_API_KEY", "change-me")

def require_auth(f):
    def decorated(*args, **kwargs):
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth[7:]
        if token != API_KEY:
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated

SYMBOLS = ["XRPUSDT", "SOLUSDT"]
TIMEFRAMES = {"1m": "1m", "5m": "5m", "15m": "15m", "1h": "1h", "4h": "4h"}
BINANCE_URL = "https://data-api.binance.vision/api/v3/klines"

def fetch_klines(symbol, interval):
    """Trae velas FRESCAS de Binance. Sin cache."""
    url = f"{BINANCE_URL}?symbol={symbol}&interval={interval}&limit=30"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()

def vela_dict(k):
    """Convierte vela cruda a dict."""
    return {
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "open_time_utc": datetime.utcfromtimestamp(int(k[0])/1000).isoformat() + "Z",
        "close_time_utc": datetime.utcfromtimestamp(int(k[6])/1000).isoformat() + "Z",
    }

def detectar_patron(v):
    """Detecta patrón: doji, martillo, marubozu, etc."""
    o, h, l, c = v["open"], v["high"], v["low"], v["close"]
    rango = h - l
    if rango == 0:
        return "sin_rango", {"cuerpo_pct": 0, "mecha_sup_pct": 0, "mecha_inf_pct": 0}
    
    cuerpo = abs(c - o)
    techo = max(o, c)
    suelo = min(o, c)
    mecha_sup = h - techo
    mecha_inf = suelo - l
    
    cuerpo_pct = round(cuerpo / rango * 100, 1)
    mecha_sup_pct = round(mecha_sup / rango * 100, 1)
    mecha_inf_pct = round(mecha_inf / rango * 100, 1)
    
    detalle = {
        "cuerpo_pct": cuerpo_pct,
        "mecha_sup_pct": mecha_sup_pct,
        "mecha_inf_pct": mecha_inf_pct,
        "alcista": c > o,
    }
    
    if cuerpo_pct <= 10:
        return "doji", detalle
    if mecha_inf_pct >= 50 and cuerpo_pct <= 35 and mecha_sup_pct <= 10:
        return "martillo", detalle
    if mecha_sup_pct >= 50 and cuerpo_pct <= 35 and mecha_inf_pct <= 10:
        return "estrella_fugaz", detalle
    if cuerpo_pct >= 90:
        return "marubozu_alcista" if c > o else "marubozu_bajista", detalle
    
    return "normal", detalle

def posicion_rango(precio, velas):
    """Posición en rango de últimas 20 velas cerradas."""
    cerradas = velas[:-1][-20:]
    if len(cerradas) < 2:
        return None
    
    highs = [float(k[2]) for k in cerradas]
    lows = [float(k[3]) for k in cerradas]
    max_r = max(highs)
    min_r = min(lows)
    
    if max_r == min_r:
        return None
    
    pct = (precio - min_r) / (max_r - min_r) * 100
    return {
        "posicion_pct": round(pct, 1),
        "maximo_reciente": max_r,
        "minimo_reciente": min_r,
    }

@app.route("/precio/<symbol>")
@require_auth
def precio_symbol(symbol):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        return jsonify({"error": "simbolo no soportado"}), 400
    
    resultado = {
        "simbolo": symbol,
        "timestamp_utc": datetime.utcnow().isoformat() + "Z",
        "timeframes": {},
    }
    
    for label, interval in TIMEFRAMES.items():
        try:
            raw = fetch_klines(symbol, interval)
            if len(raw) < 3:
                resultado["timeframes"][label] = {"error": "datos_insuficientes"}
                continue
            
            vela_actual = vela_dict(raw[-1])
            vela_cerrada = vela_dict(raw[-2])
            vela_previa = vela_dict(raw[-3])
            
            patron, detalle = detectar_patron(vela_cerrada)
            rango = posicion_rango(vela_actual["close"], raw)
            
            resultado["timeframes"][label] = {
                "tf": label,
                "vela_actual": {**vela_actual, "estado": "en_curso"},
                "vela_anterior": {**vela_cerrada, "estado": "cerrada", "patron": patron, **detalle},
                "posicion_en_rango": rango,
            }
        except Exception as e:
            resultado["timeframes"][label] = {"error": str(e)}
    
    return jsonify(resultado)

@app.route("/")
def home():
    return jsonify({"servicio": "PALMERO Accion del Precio", "status": "ok"})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
