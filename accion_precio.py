"""
PALMERO - Especialista en Accion del Precio
=============================================
Servicio independiente que analiza velas (OHLC) por simbolo y timeframe:
- Vela actual (en formacion) y vela anterior (cerrada)
- Patron detectado en la vela cerrada (martillo, envolvente, doji, etc.)
- Tamano relativo de cuerpo y mechas
- Posicion respecto al rango reciente (cerca de maximos/minimos)
- Timestamps de apertura/cierre de cada vela y de generacion de la respuesta

No depende de los servicios existentes (superb-growth, sincere-gentleness,
palmero-divergencias). Expone un endpoint JSON que Claude puede consultar
bajo demanda (pegando el link en el chat).

Despliegue: Railway, como servicio nuevo independiente.
"""

import os
import time
import requests
import numpy as np
from datetime import datetime, timezone
from flask import Flask, jsonify

app = Flask(__name__)
@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    return response
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
NUM_VELAS = 30  # ventana para contexto y rango reciente

# Cache simple en memoria (evita golpear Binance en cada request)
_cache = {}
_cache_ttl = 20  # segundos


def fetch_klines(symbol, interval, limit=NUM_VELAS):
    """Descarga velas OHLCV de Binance publico, incluyendo la vela en curso."""
    key = (symbol, interval)
    now = time.time()
    if key in _cache and now - _cache[key]["ts"] < _cache_ttl:
        return _cache[key]["data"]

    url = f"{BINANCE_BASE}?symbol={symbol}&interval={interval}&limit={limit}"
    resp = requests.get(url, timeout=10)
    resp.raise_for_status()
    raw = resp.json()
    # raw[-1] es la vela actual (en formacion), raw[-2] es la ultima cerrada

    _cache[key] = {"ts": now, "data": raw}
    return raw


def vela_to_dict(k):
    """Convierte una vela cruda de Binance a un diccionario con OHLCV y tiempos."""
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
    """
    Detecta patrones basicos de una sola vela a partir de OHLC.
    Devuelve (patron, detalle) donde detalle incluye tamanos relativos.
    """
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

    # Doji: cuerpo muy pequeno
    if cuerpo_pct <= 10:
        return "doji", detalle

    # Martillo: mecha inferior grande (>=50%), cuerpo pequeno (<=35%), mecha superior pequena (<=10%)
    if mecha_inf_pct >= 50 and cuerpo_pct <= 35 and mecha_sup_pct <= 10:
        return "martillo", detalle

    # Estrella fugaz / martillo invertido: mecha superior grande, cuerpo pequeno, mecha inferior pequena
    if mecha_sup_pct >= 50 and cuerpo_pct <= 35 and mecha_inf_pct <= 10:
        return "estrella_fugaz", detalle

    # Marubozu: cuerpo ocupa casi todo el rango (>=90%)
    if cuerpo_pct >= 90:
        return "marubozu_alcista" if alcista else "marubozu_bajista", detalle

    return "normal", detalle


def detectar_envolvente(vela_actual, vela_anterior):
    """
    Detecta si la vela cerrada mas reciente es una envolvente
    (alcista o bajista) respecto a la anterior a ella.
    """
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
    """
    Calcula en que punto del rango reciente (maximo/minimo de las
    ultimas `window` velas cerradas) se encuentra el precio actual.
    Devuelve un porcentaje: 0 = en el minimo, 100 = en el maximo.
    """
    cerradas = velas[-(window + 1):-1]  # excluye la vela en curso
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

    vela_actual_raw = raw[-1]   # en formacion
    vela_cerrada_raw = raw[-2]  # ultima cerrada
    vela_previa_raw = raw[-3]   # anterior a la cerrada (para envolvente)

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
        "servicio": "PALMERO - Accion del Precio",
        "endpoints": [
            "/precio - todos los simbolos y timeframes",
            "/precio/<symbol> - ej. /precio/XRPUSDT",
        ],
        "nota": "vela_actual = en formacion (sin cerrar). vela_anterior = ultima cerrada, con patron detectado.",
    })


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
