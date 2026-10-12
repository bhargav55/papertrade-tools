"""Queue-risk / LP-health monitor with sqlite history."""
from __future__ import annotations
import json, os, sqlite3, time, threading
from pathlib import Path
from settlement import WAD, settle_close, InstrumentParams
from chain import (
    get_live_params, hl_bbo_mid, protocol_balances, oi_from_trading,
    fetch_queue_summary, fetch_house_current, fetch_trading_state, SITE,
)
import httpx

_DATA_ROOT = Path(os.environ.get("DATA_DIR") or (Path(__file__).parent / "data"))
DATA = _DATA_ROOT
DB = DATA / "monitor.db"
ALERT_LOG = DATA / "alerts.log"
POLL_S = 15

def _conn():
    DATA.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB, check_same_thread=False)
    c.execute("""CREATE TABLE IF NOT EXISTS samples(
        ts REAL PRIMARY KEY,
        lp REAL, margin REAL, queue REAL, tvl REAL,
        house_pnl REAL, headroom REAL, headroom_pct REAL,
        btc_mid REAL, eth_mid REAL,
        btc_long_oi REAL, btc_short_oi REAL,
        eth_long_oi REAL, eth_short_oi REAL,
        site_house_pnl REAL,
        payload TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts(
        ts REAL, kind TEXT, message TEXT, payload TEXT
    )""")
    c.commit()
    return c

def _site_get(path: str):
    r = httpx.get(SITE + path, headers={"User-Agent": "Mozilla/5.0 PapertradeTools/1.0"}, timeout=25)
    r.raise_for_status()
    return r.json()

def house_delta(coin: str) -> dict:
    return _site_get(f"/state/house/delta?coin={coin}")["delta"]

def site_house_pnl(house: dict) -> float | None:
    head = house.get("head")
    if head and len(head) >= 3:
        return float(head[2])
    return None

def oi_from_house_head(house: dict) -> dict:
    """head: [ts, lp, pnl, q, btcLong, btcShort, ethLong, ethShort]"""
    h = house.get("head") or []
    if len(h) < 8:
        return {}
    return {
        "BTC": {"long_oi": float(h[4]), "short_oi": float(h[5])},
        "ETH": {"long_oi": float(h[6]), "short_oi": float(h[7])},
    }

def stress_from_deltas(site_pnl: float, btc_d: dict, eth_d: dict) -> list[dict]:
    """delta.values are house-PnL *change* vs mark; mid index == 0."""
    def idx_for(delta: dict, move_pct: float) -> int:
        # samples span [-range, +range] as fraction (range=0.01 => ±1%)
        rng = float(delta["range"])
        n = len(delta["values"])
        if rng <= 0 or n < 2:
            return n // 2
        # move_pct is in percent; convert to fraction
        frac = move_pct / 100.0
        # clamp to range
        frac = max(-rng, min(rng, frac))
        # i = (frac + range) / (2*range) * (n-1)
        i = int(round((frac + rng) / (2 * rng) * (n - 1)))
        return max(0, min(n - 1, i))

    rows = []
    for m in [0.25, 0.5, 1.0]:
        # 2% requested but API range is ±1%; include 2% clamped note
        for label, bm, em in [
            (f"BTC +{m}%", m, 0.0),
            (f"BTC -{m}%", -m, 0.0),
            (f"ETH +{m}%", 0.0, m),
            (f"ETH -{m}%", 0.0, -m),
            (f"Both +{m}%", m, m),
            (f"Both -{m}%", -m, -m),
        ]:
            db = btc_d["values"][idx_for(btc_d, bm)]
            de = eth_d["values"][idx_for(eth_d, em)]
            rows.append({
                "label": label,
                "btc_move_pct": bm,
                "eth_move_pct": em,
                "house_pnl_usd": site_pnl + db + de,
                "delta_usd": db + de,
                "clamped": abs(bm) > btc_d["range"] * 100 or abs(em) > eth_d["range"] * 100,
            })
    # explicit ±1% already included; add ±2% as clamped-to-1% with flag
    for m in [2.0]:
        for label, bm, em in [
            (f"BTC +{m}% (clamped to API ±1%)", m, 0.0),
            (f"BTC -{m}% (clamped to API ±1%)", -m, 0.0),
            (f"ETH +{m}% (clamped to API ±1%)", 0.0, m),
            (f"ETH -{m}% (clamped to API ±1%)", 0.0, -m),
            (f"Both +{m}% (clamped to API ±1%)", m, m),
            (f"Both -{m}% (clamped to API ±1%)", -m, -m),
        ]:
            db = btc_d["values"][idx_for(btc_d, bm)]
            de = eth_d["values"][idx_for(eth_d, em)]
            rows.append({
                "label": label,
                "btc_move_pct": bm,
                "eth_move_pct": em,
                "house_pnl_usd": site_pnl + db + de,
                "delta_usd": db + de,
                "clamped": True,
            })
    return rows

def load_open_from_parquet() -> list[dict]:
    op = Path(__file__).resolve().parents[1] / "opens.parquet"
    cl = Path(__file__).resolve().parents[1] / "closes.parquet"
    if not op.exists() or not cl.exists():
        return []
    import pandas as pd
    o = pd.read_parquet(op)
    c = pd.read_parquet(cl)
    closed = set(c.pid.tolist())
    open_rows = o[~o.pid.isin(closed)]
    out = []
    for _, r in open_rows.iterrows():
        out.append({
            "instrument": "BTC" if int(r.inst) == 0 else "ETH",
            "is_long": int(r.long) == 1,
            "entry_raw": int(r.entry),
            "margin_raw": int(round(float(r.margin) * WAD)),
            "leverage": int(r.lev),
            "pid": int(r.pid),
        })
    return out

def mark_open_house_pnl(opens: list[dict], mids: dict[str, float], params: dict[str, InstrumentParams]) -> dict:
    from settlement import to_raw_price
    trader_net = 0.0
    by_asset = {"BTC": 0.0, "ETH": 0.0}
    for pos in opens:
        asset = pos["instrument"]
        p = params[asset]
        exit_px = to_raw_price(mids[asset], p.price_scale)
        r = settle_close(pos["margin_raw"], pos["leverage"], pos["entry_raw"], exit_px, pos["is_long"], p)
        net = r["net"] / WAD
        trader_net += net
        by_asset[asset] += net
    return {
        "open_count": len(opens),
        "trader_net_usd": trader_net,
        "house_pnl_usd": -trader_net,
        "by_asset": by_asset,
    }

def sample_once(conn: sqlite3.Connection | None = None) -> dict:
    own = conn is None
    conn = conn or _conn()
    params = get_live_params(force=True)
    bal = protocol_balances()
    try:
        q = fetch_queue_summary()
        queue = int(q.get("total") or 0) / WAD
    except Exception:
        queue = bal["queue"]
        q = {}
    trading = fetch_trading_state()
    oi_trading = oi_from_trading(trading)
    house = fetch_house_current()
    site_pnl = site_house_pnl(house) or 0.0
    oi_head = oi_from_house_head(house)
    oi = {
        "BTC": {**(oi_trading.get("BTC") or {}), **(oi_head.get("BTC") or {})},
        "ETH": {**(oi_trading.get("ETH") or {}), **(oi_head.get("ETH") or {})},
    }
    btc = hl_bbo_mid("BTC")
    eth = hl_bbo_mid("ETH")
    mids = {"BTC": btc["mid"], "ETH": eth["mid"]}

    btc_d = house_delta("BTC")
    eth_d = house_delta("ETH")
    stress = stress_from_deltas(site_pnl, btc_d, eth_d)

    local_opens = load_open_from_parquet()
    local_mark = mark_open_house_pnl(local_opens, mids, params) if local_opens else None

    lp = bal["lp"]
    # Use site open house PnL (matches House page). Loss if negative.
    house_pnl = site_pnl
    house_loss_if_close = max(0.0, -house_pnl)
    headroom = lp - house_loss_if_close - queue
    headroom_pct = (headroom / lp * 100.0) if lp > 0 else 0.0

    payload = {
        "ts": time.time(),
        "balances": bal,
        "queue_debt": queue,
        "queue_raw": q,
        "oi": oi,
        "mids": mids,
        "bbo": {"BTC": btc, "ETH": eth},
        "house_pnl_usd": house_pnl,
        "site_house_pnl_usd": site_pnl,
        "local_mark": local_mark,
        "delta_meta": {
            "BTC": {"mark": btc_d["mark"], "range": btc_d["range"], "positionCount": btc_d["positionCount"]},
            "ETH": {"mark": eth_d["mark"], "range": eth_d["range"], "positionCount": eth_d["positionCount"]},
        },
        "headroom_usd": headroom,
        "headroom_pct": headroom_pct,
        "stress": stress,
        "params_bust_bp": {k: v.bust_buffer / WAD * 1e4 for k, v in params.items()},
        "open_source": "site_house_head+delta",
        "open_mark": {
            "open_count": (btc_d.get("positionCount") or 0) + (eth_d.get("positionCount") or 0),
            "house_pnl_usd": house_pnl,
            "note": "Absolute open house PnL from /state/house/current head[2]; stress via /state/house/delta",
        },
    }

    alerts = []
    if headroom_pct < 25 or headroom < 0:
        alerts.append(("headroom", f"Headroom {headroom:,.0f} USD ({headroom_pct:.1f}% of LP {lp:,.0f})"))
    if queue > 0:
        alerts.append(("queue", f"Queue debt {queue:,.2f} USD"))

    ts = payload["ts"]
    conn.execute(
        "INSERT OR REPLACE INTO samples VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            ts, lp, bal["margin"], queue, bal["tvl"], house_pnl, headroom, headroom_pct,
            mids["BTC"], mids["ETH"],
            oi.get("BTC", {}).get("long_oi"), oi.get("BTC", {}).get("short_oi"),
            oi.get("ETH", {}).get("long_oi"), oi.get("ETH", {}).get("short_oi"),
            site_pnl, json.dumps(payload),
        ),
    )
    for kind, msg in alerts:
        conn.execute("INSERT INTO alerts VALUES (?,?,?,?)", (ts, kind, msg, json.dumps({"kind": kind})))
        with open(ALERT_LOG, "a") as f:
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S %Z', time.localtime(ts))} [{kind}] {msg}\n")
    conn.commit()
    payload["alerts"] = [{"kind": k, "message": m} for k, m in alerts]
    if own:
        conn.close()
    return payload

def history(limit: int = 200) -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT ts,lp,margin,queue,tvl,house_pnl,headroom,headroom_pct,btc_mid,eth_mid,site_house_pnl FROM samples ORDER BY ts DESC LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    keys = ["ts","lp","margin","queue","tvl","house_pnl","headroom","headroom_pct","btc_mid","eth_mid","site_house_pnl"]
    return [dict(zip(keys, r)) for r in reversed(rows)]

def alerts(limit: int = 50) -> list[dict]:
    conn = _conn()
    rows = conn.execute("SELECT ts,kind,message FROM alerts ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [{"ts": r[0], "kind": r[1], "message": r[2]} for r in rows]

_stop = threading.Event()
_thread: threading.Thread | None = None

def _loop():
    conn = _conn()
    while not _stop.is_set():
        try:
            sample_once(conn)
        except Exception as e:
            with open(ALERT_LOG, "a") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [error] {e}\n")
        _stop.wait(POLL_S)
    conn.close()

def start_background():
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="lp-monitor", daemon=True)
    _thread.start()
