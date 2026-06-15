"""
PALMERO - Especialista en Accion del Precio (CON AUTENTICACION)
=============================================
Versión segura con API key authentication.
"""

import os
import time
import requests
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify, request

app = Flask(__name__)

# ============================================================
# AUTENTICACION
# ============================================================
API_KEY = os.environ.get("PALMERO_API_KEY", "default-change-me")

def check_auth():
    """Verifica que el request tenga la API key correcta."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return False
    token = auth_header[7:]  # Quita "Bearer "
    return token == API_KEY

def require_auth(f):
    """Decorador para proteger endpoints."""
    def decorated(*args, **kwargs):
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    decorated.__name__ = f.__name__
    return decorated

# Configuracion
SYMBOLS = ["XRPUSDT", "SOLUSDT"]
TIMEFRAMES = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "1h",
    "4h": "4h",
}
BINANCE_BASE = "https://data-api.binance.vision/api/v3/klines"
NUM_VELAS = 30

_cache = {}
_cache_ttl = 20


def fetch_klines(symbol, interval, limit=NUM_VELAS):
    """Descarga velas OHLCV de Binance publico."""
    key = (symbol, interval)
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < _cache_ttl:
        return _cache[key]["data"]

    url = f"{BINANCE_BASE}?symbol={symbol}&interval={interval}&limit={limit}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    raw = resp.json()

    _cache[key] = {"ts": now, "data": raw}
    return raw


def vela_to_dict(k):
    """Convierte una vela cruda de Binance a un diccionario."""
    open_time = int(k[0])
    close_time = int(k[6])
    return {
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5]),
        "open_time_utc": datetime.fromtimestamp(open_time / 1000, tz=timezone.utc).isoformat(),
        "close_time_utc": datetime.fromtimestamp(close_time / 1000, tz=timezone.utc).isoformat(),
    }


def detectar_patron(vela):
    """Detecta patrones basicos de una sola vela."""
    o, h, l, c = vela["open"], vela["high"], vela["low"], vela["close"]
    rango_total = h - l
    if rango_total == 0:
        return "sin_rango", {
            "cuerpo_pct": 0, "mecha_sup_pct": 0, "mecha_inf_pct": 0,
        }

    cuerpo = abs(c - o)
    techo_cuerpo = max(o, c)
    suelo_cuerpo = min(o, c)
    mecha_sup = h - techo_cuerpo
    mecha_inf = suelo_cuerpo - l

    cuerpo_pct = round(cuerpo / rango_total * 100, 1)
    mecha_sup_pct = round(mecha_sup / rango_total * 100, 1)
    mecha_inf_pct = round(mecha_inf / rango_total * 100, 1)

    alcista = c > o
    detalle = {
        "cuerpo_pct": cuerpo_pct,
        "mecha_sup_pct": mecha_sup_pct,
        "mecha_inf_pct": mecha_inf_pct,
        "alcista": alcista,
    }

    if cuerpo_pct <= 10:
        return "doji", detalle
    if mecha_inf_pct >= 50 and cuerpo_pct <= 35 and mecha_sup_pct <= 10:
        return "martillo", detalle
    if mecha_sup_pct >= 50 and cuerpo_pct <= 35 and mecha_inf_pct <= 10:
        return "estrella_fugaz", detalle
    if cuerpo_pct >= 90:
        return "marubozu_alcista" if alcista else "marubozu_bajista", detalle

    return "normal", detalle


def detectar_envolvente(vela_actual, vela_anterior):
    """Detecta envolvente alcista o bajista."""
    o1, c1 = vela_anterior["open"], vela_anterior["close"]
    o2, c2 = vela_actual["open"], vela_actual["close"]

    cuerpo1_min, cuerpo1_max = min(o1, c1), max(o1, c1)
    cuerpo2_min, cuerpo2_max = min(o2, c2), max(o2, c2)

    envuelve = cuerpo2_min <= cuerpo1_min and cuerpo2_max >= cuerpo1_max

    if not envuelve:
        return "ninguna"

    alcista_ant = c1 < o1
    alcista_act = c2 > o2

    if alcista_ant and alcista_act:
        return "envolvente_alcista"
    if (not alcista_ant) and (not alcista_act):
        return "envolvente_bajista"
    return "ninguna"


def posicion_en_rango(precio, velas, window=20):
    """Calcula posicion en rango reciente."""
    cerradas = velas[-(window + 1):-1]
    if len(cerradas) < 2:
        return None

    highs = [float(k[2]) for k in cerradas]
    lows = [float(k[3]) for k in cerradas]
    max_reciente = max(highs)
    min_reciente = min(lows)

    if max_reciente == min_reciente:
        return None

    pct = (precio - min_reciente) / (max_reciente - min_reciente) * 100
    return {
        "posicion_pct": round(pct, 1),
        "maximo_reciente": max_reciente,
        "minimo_reciente": min_reciente,
        "ventana_velas": len(cerradas),
    }


def analizar_simbolo_tf(symbol, tf_label, interval):
    raw = fetch_klines(symbol, interval)
    if len(raw) < 3:
        return {"error": "datos_insuficientes"}

    vela_actual_raw = raw[-1]
    vela_cerrada_raw = raw[-2]
    vela_previa_raw = raw[-3]

    vela_actual = vela_to_dict(vela_actual_raw)
    vela_cerrada = vela_to_dict(vela_cerrada_raw)
    vela_previa = vela_to_dict(vela_previa_raw)

    patron, detalle = detectar_patron(vela_cerrada)
    envolvente = detectar_envolvente(vela_cerrada, vela_previa)

    precio_actual = vela_actual["close"]
    rango = posicion_en_rango(precio_actual, raw, window=20)

    return {
        "tf": tf_label,
        "vela_actual": {
            **vela_actual,
            "estado": "en_curso",
        },
        "vela_anterior": {
            **vela_cerrada,
            "estado": "cerrada",
            "patron": patron,
            "envolvente": envolvente,
            **detalle,
        },
        "posicion_en_rango": rango,
    }


@app.route("/precio/<symbol>")
@require_auth
def precio_symbol(symbol):
    symbol = symbol.upper()
    if symbol not in SYMBOLS:
        return jsonify({"error": f"simbolo no soportado: {symbol}"}), 400

    resultado = {
        "simbolo": symbol,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "timeframes": {},
    }
    for label, interval in TIMEFRAMES.items():
        try:
            resultado["timeframes"][label] = analizar_simbolo_tf(symbol, label, interval)
        except Exception as e:
            resultado["timeframes"][label] = {"error": str(e)}

    return jsonify(resultado)


@app.route("/precio")
@require_auth
def precio_todos():
    resultado = {"timestamp_utc": datetime.now(timezone.utc).isoformat()}
    for symbol in SYMBOLS:
        resultado[symbol] = {}
        for label, interval in TIMEFRAMES.items():
            try:
                resultado[symbol][label] = analizar_simbolo_tf(symbol, label, interval)
            except Exception as e:
                resultado[symbol][label] = {"error": str(e)}
    return jsonify(resultado)


@app.route("/")
def home():
    return jsonify({
        "servicio": "PALMERO - Accion del Precio (SECURED)",
        "nota": "endpoints protegidos con API key. Incluir header: Authorization: Bearer {PALMERO_API_KEY}",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
