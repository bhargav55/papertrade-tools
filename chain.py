"""Live Hyperliquid BBO + Papertrade on-chain / public API params."""
from __future__ import annotations
import time
import httpx
from settlement import InstrumentParams, DEFAULT_PARAMS, WAD

RPCS = [
    "https://rpc.hypurrscan.io",
    "https://rpc.hyperliquid.xyz/evm",
    "https://999.rpc.thirdweb.com",
]
EXCHANGE = "0x6cd5661646289FB6E65EA5C032310fDEd797D0a2"
HL_INFO = "https://api.hyperliquid.xyz/info"
SITE = "https://exchange.papertrade.xyz"

_cache: dict = {"t": 0.0, "params": None, "summary": None, "trading": None, "house": None}

def _rpc(method: str, params: list, timeout: float = 20.0):
    last = None
    for url in RPCS:
        try:
            r = httpx.post(url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=timeout)
            j = r.json()
            if "result" in j:
                return j["result"]
            last = j
        except Exception as e:
            last = e
    raise RuntimeError(f"RPC failed: {last}")

def eth_call(to: str, data: str) -> str:
    return _rpc("eth_call", [{"to": to, "data": data}, "latest"])

def hl_mids() -> dict[str, float]:
    r = httpx.post(HL_INFO, json={"type": "allMids"}, timeout=15)
    r.raise_for_status()
    d = r.json()
    return {k: float(v) for k, v in d.items()}

def hl_bbo_mid(coin: str) -> dict:
    """Best-effort BBO mid: prefer l2Book mid, fallback allMids."""
    try:
        r = httpx.post(HL_INFO, json={"type": "l2Book", "coin": coin}, timeout=15)
        r.raise_for_status()
        book = r.json()
        levels = book.get("levels") or book
        # levels: [bids, asks] each [[px,sz],...]
        if isinstance(levels, list) and len(levels) >= 2:
            bid = float(levels[0][0]["px"] if isinstance(levels[0][0], dict) else levels[0][0][0])
            ask = float(levels[1][0]["px"] if isinstance(levels[1][0], dict) else levels[1][0][0])
            return {"bid": bid, "ask": ask, "mid": (bid + ask) / 2, "source": "l2Book"}
    except Exception:
        pass
    mids = hl_mids()
    mid = mids[coin]
    return {"bid": mid, "ask": mid, "mid": mid, "source": "allMids"}

def _site_get(path: str):
    r = httpx.get(SITE + path, headers={"User-Agent": "Mozilla/5.0 PapertradeTools/1.0"}, timeout=20)
    r.raise_for_status()
    return r.json()

def fetch_trading_state() -> dict:
    return _site_get("/state/trading")

def fetch_protocol_summary() -> dict:
    return _site_get("/query/protocol/summary")

def fetch_house_current() -> dict:
    return _site_get("/state/house/current")

def fetch_queue_summary() -> dict:
    return _site_get("/query/protocol/queue-summary")

def parse_instrument_params(trading: dict) -> dict[str, InstrumentParams]:
    out = dict(DEFAULT_PARAMS)
    for row in trading.get("instruments") or []:
        # [id, sym, ..., rateMult, posMult, maxLev, maxNotional, bustBuffer, baseRate, refNotional, longOi, shortOi, longCap, shortCap]
        try:
            sym = row[1]
            if sym not in ("BTC", "ETH"):
                continue
            price_scale = int(row[3])
            rate_mult = int(row[12])
            pos_mult = int(row[13])
            max_lev = int(row[14])
            bust = int(row[16])
            base = int(row[17])
            ref = int(row[18])
            out[sym] = InstrumentParams(
                symbol=sym,
                price_scale=price_scale,
                rate_multiplier=rate_mult,
                position_multiplier=pos_mult,
                base_rate=base,
                reference_notional=ref,
                bust_buffer=bust,
                max_leverage=max_lev,
            )
        except Exception:
            continue
    return out

def get_live_params(force: bool = False) -> dict[str, InstrumentParams]:
    now = time.time()
    if not force and _cache["params"] and now - _cache["t"] < 30:
        return _cache["params"]
    try:
        trading = fetch_trading_state()
        params = parse_instrument_params(trading)
        _cache["trading"] = trading
        _cache["params"] = params
        _cache["t"] = now
        return params
    except Exception:
        return dict(DEFAULT_PARAMS)

def oi_from_trading(trading: dict | None = None) -> dict:
    trading = trading or _cache.get("trading") or fetch_trading_state()
    out = {}
    for row in trading.get("instruments") or []:
        sym = row[1]
        out[sym] = {
            "long_oi": int(row[19]) / WAD,
            "short_oi": int(row[20]) / WAD,
            "long_cap": int(row[21]) / WAD,
            "short_cap": int(row[22]) / WAD,
        }
    return out

def protocol_balances() -> dict:
    s = fetch_protocol_summary()
    b = s["balances"]
    return {
        "tvl": int(b["tvl"]) / WAD,
        "margin": int(b["margin"]) / WAD,
        "lp": int(b["lp"]) / WAD,
        "reserve": int(b.get("reserve") or 0) / WAD,
        "queue": int(b["queue"]) / WAD,
        "block": s["source"]["block"],
        "at_ms": s["source"]["at"],
        "volume": int(s["activity"]["volume"]) / WAD,
        "open_positions": s["activity"]["open"],
        "raw": s,
    }
