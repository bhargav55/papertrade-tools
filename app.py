#!/usr/bin/env python3
"""Papertrade tools: haircut calculator + LP/queue monitor."""
from __future__ import annotations
import os
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory
import calculator
import monitor

app = Flask(__name__, static_folder="static", static_url_path="")

@app.get("/")
def index():
    return send_from_directory("static", "index.html")

@app.get("/api/health")
def health():
    return {"ok": True, "service": "papertrade-tools"}

@app.get("/api/calc")
@app.post("/api/calc")
def calc():
    src = request.get_json(silent=True) if request.method == "POST" else request.args
    src = src or {}
    try:
        asset = src.get("asset", "BTC")
        direction = src.get("direction", "long")
        margin = float(src.get("margin", src.get("margin_usd", 100)))
        leverage = int(src.get("leverage", 100))
        entry = src.get("entry", src.get("entry_usd"))
        entry_f = float(entry) if entry not in (None, "") else None
        result = calculator.calculate(asset, direction, margin, leverage, entry_f)
        return jsonify({"ok": True, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.get("/api/monitor")
@app.get("/api/monitor/latest")
def monitor_latest():
    try:
        # return last sample or take one now
        hist = monitor.history(1)
        if hist:
            # reload full payload from db
            import sqlite3, json
            conn = monitor._conn()
            row = conn.execute("SELECT payload FROM samples ORDER BY ts DESC LIMIT 1").fetchone()
            conn.close()
            if row:
                return jsonify({"ok": True, "sample": json.loads(row[0])})
        sample = monitor.sample_once()
        return jsonify({"ok": True, "sample": sample})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.post("/api/monitor/sample")
def monitor_sample():
    try:
        return jsonify({"ok": True, "sample": monitor.sample_once()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.get("/api/monitor/history")
def monitor_history():
    limit = int(request.args.get("limit", 200))
    return jsonify({"ok": True, "history": monitor.history(limit)})

@app.get("/api/alerts")
def api_alerts():
    return jsonify({"ok": True, "alerts": monitor.alerts(int(request.args.get("limit", 50)))})

@app.get("/api/mids")
def api_mids():
    from chain import hl_bbo_mid
    return jsonify({"ok": True, "BTC": hl_bbo_mid("BTC"), "ETH": hl_bbo_mid("ETH")})

def main():
    Path(os.environ.get("DATA_DIR", "data")).mkdir(parents=True, exist_ok=True)
    monitor.start_background()
    port = int(os.environ.get("PORT", "8787"))
    app.run(host="0.0.0.0", port=port, threaded=True)

if __name__ == "__main__":
    main()
