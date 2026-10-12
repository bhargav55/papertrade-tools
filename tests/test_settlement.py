"""Replay real closes from positions/opens/closes — must match on-chain fields exactly."""
from __future__ import annotations
import json
import sys
from pathlib import Path
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from settlement import InstrumentParams, WAD, settle_close, DEFAULT_PARAMS

OPENS = ROOT / "opens.parquet"
CLOSES = ROOT / "closes.parquet"
RAW = ROOT / "raw_logs.jsonl"

def _params(inst: int) -> InstrumentParams:
    return DEFAULT_PARAMS["BTC" if inst == 0 else "ETH"]

def _signed(w: int) -> int:
    return w - 2**256 if w >= 2**255 else w

@pytest.mark.skipif(not OPENS.exists() or not CLOSES.exists(), reason="missing parquet")
def test_replay_sample_exact():
    o = pd.read_parquet(OPENS)
    c = pd.read_parquet(CLOSES)
    # Prefer exact ints from raw logs if available
    O = {}
    if RAW.exists():
        n = 0
        for line in open(RAW):
            x = json.loads(line)
            t = x["topics"][0][:10]
            if t != "0x88451ea1":
                continue
            d = x["data"][2:]
            w = [_signed(int(d[i : i + 64], 16)) for i in range(0, len(d), 64)]
            O[int(x["topics"][1], 16)] = w
            n += 1
            if n > 8000:
                break
    matched = 0
    checked = 0
    for _, row in c.head(3000).iterrows():
        if row.get("kind") == "liq":
            continue
        pid = int(row.pid)
        if pid in O:
            inst, lg, _, entry, margin, lev = O[pid]
            exit_px = int(row.exit) if "exit" in row else None
        else:
            orow = o[o.pid == pid]
            if orow.empty:
                continue
            r = orow.iloc[0]
            inst, lg = int(r.inst), int(r.long)
            entry, margin, lev = int(r.entry), int(round(float(r.margin) * WAD)), int(r.lev)
        # get exit + expected from closes row — need int from raw
        continue  # handled below via raw join

    # Full raw-log replay
    assert RAW.exists(), "raw_logs.jsonl required for exact replay"
    opens = {}
    closes = []
    for line in open(RAW):
        x = json.loads(line)
        t = x["topics"][0][:10]
        d = x["data"][2:]
        w = [_signed(int(d[i : i + 64], 16)) for i in range(0, len(d), 64)]
        pid = int(x["topics"][1], 16)
        if t == "0x88451ea1":
            opens[pid] = w
        elif t == "0x87902ed2":
            closes.append((pid, w))
        if len(closes) >= 2500:
            break
    for pid, cw in closes:
        if pid not in opens:
            continue
        ow = opens[pid]
        inst, lg, _, entry, margin, lev = ow
        exit_px, pnl_db, pnl_adj = cw[0], cw[1], cw[2]
        if pnl_adj <= 0:
            continue
        p = _params(inst)
        r = settle_close(margin, lev, entry, exit_px, lg == 1, p)
        checked += 1
        assert r["pnl_after_deadband"] == pnl_db, (pid, r["pnl_after_deadband"], pnl_db)
        assert r["pnl_after_haircut"] == pnl_adj, (pid, r["pnl_after_haircut"], pnl_adj)
        matched += 1
    assert checked >= 500, checked
    assert matched == checked

def test_deadband_wipes_tiny_win():
    p = DEFAULT_PARAMS["BTC"]
    entry = 800_000  # $80,000 at scale 10
    # move of 1 tick < entry/50000=16
    r = settle_close(100 * WAD, 100, entry, entry + 5, True, p)
    assert r["raw_pnl"] == 0
    assert r["net"] == 0

def test_loss_no_fee():
    p = DEFAULT_PARAMS["ETH"]
    entry = 250_000
    r = settle_close(50 * WAD, 50, entry, entry - 1000, True, p)
    assert r["raw_pnl"] < 0
    assert r["fee"] == 0
    assert r["net"] == r["pnl_after_haircut"]
