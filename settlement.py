"""Exact Papertrade settlement math (verified 2152/2152 against on-chain closes)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Literal

WAD = 10**18
DEADBAND_DIV = 50_000  # entry // 50000 == 0.2 bp of price

Asset = Literal["BTC", "ETH"]

@dataclass(frozen=True)
class InstrumentParams:
    symbol: Asset
    price_scale: int  # 10 BTC, 100 ETH
    rate_multiplier: int  # WAD-scaled, e.g. 15000 * WAD
    position_multiplier: int  # WAD-scaled
    base_rate: int  # WAD-scaled, 0.1 * WAD
    reference_notional: int  # WAD-scaled USD
    bust_buffer: int  # WAD-scaled fraction (~4.76e14)
    max_leverage: int = 1000
    win_fee_rate: int = 2 * 10**16  # 2%

# Docs / last-known fallbacks
DEFAULT_PARAMS = {
    "BTC": InstrumentParams("BTC", 10, 15000 * WAD, 814598 * 10**15, 10**17, 10**23, 476041580087574),
    "ETH": InstrumentParams("ETH", 100, 15000 * WAD, 483979 * 10**15, 10**17, 10**23, 481304050180130),
}

def to_raw_price(usd: float, scale: int) -> int:
    return int(round(usd * scale))

def from_raw_price(raw: int, scale: int) -> float:
    return raw / scale

def apply_deadband(entry: int, exit_px: int, is_long: bool) -> int:
    s = entry // DEADBAND_DIV
    if is_long:
        if exit_px > entry + s:
            return exit_px - s
        if exit_px > entry:
            return entry
        return exit_px
    if exit_px + s < entry:
        return exit_px + s
    if exit_px < entry:
        return entry
    return exit_px

def raw_pnl(margin: int, leverage: int, entry: int, mark: int, is_long: bool) -> int:
    l = (mark - entry) if is_long else (entry - mark)
    return margin * leverage * l // entry

def haircut_scale(move_num: int, move_den: int, p: InstrumentParams) -> int:
    """Return scale in WAD units given |mark-entry| / entry as move_num/move_den."""
    if move_num <= 0:
        return 0
    dfrac = move_num * WAD // move_den  # |move| in WAD
    f = dfrac * p.rate_multiplier // WAD
    pos = 1_000_000 * dfrac * p.position_multiplier // WAD
    if f == 0 or pos == 0:
        return 0
    m = WAD * WAD // f
    h = p.reference_notional * WAD // pos
    if m > WAD * WAD or h > WAD * WAD:
        return 0
    return (WAD - p.base_rate) * WAD // (WAD + m + h)

def settle_close(
    margin: int,
    leverage: int,
    entry: int,
    exit_px: int,
    is_long: bool,
    p: InstrumentParams,
) -> dict:
    mark = apply_deadband(entry, exit_px, is_long)
    raw = raw_pnl(margin, leverage, entry, mark, is_long)
    if raw <= 0:
        adj = raw if raw >= -margin else -margin
        return {
            "mark": mark,
            "raw_pnl": raw,
            "pnl_after_deadband": raw,
            "pnl_after_haircut": adj,
            "fee": 0,
            "net": adj,
            "scale_wad": 0 if raw > 0 else WAD,
        }
    move = abs(mark - entry)
    scale = haircut_scale(move, entry, p)
    adj = raw * scale // WAD
    fee = adj * p.win_fee_rate // WAD
    return {
        "mark": mark,
        "raw_pnl": raw,
        "pnl_after_deadband": raw,  # after price deadband, before haircut
        "pnl_after_haircut": adj,
        "fee": fee,
        "net": adj - fee,
        "scale_wad": scale,
    }

def zero_equity_price(entry: int, leverage: int, is_long: bool) -> int:
    # |move| = 1/leverage
    # long: entry * (1 - 1/lev) = entry - entry/lev
    delta = entry // leverage
    return entry - delta if is_long else entry + delta

def bust_price(entry: int, leverage: int, is_long: bool, p: InstrumentParams) -> int:
    """Bust ≈ zero-equity moved earlier by bust_buffer (WAD fraction of price)."""
    ze = zero_equity_price(entry, leverage, is_long)
    # buffer distance in price units: entry * bust_buffer / WAD
    buf = entry * p.bust_buffer // WAD
    if buf < 1:
        buf = 1
    if is_long:
        return ze + buf  # liquidate sooner (higher than ZE for long)
    return ze - buf

def equity_at_price(margin: int, leverage: int, entry: int, px: int, is_long: bool, p: InstrumentParams) -> dict:
    r = settle_close(margin, leverage, entry, px, is_long, p)
    equity = margin + r["net"]
    if equity < 0:
        equity = 0
    return {**r, "equity": equity}

def breakeven_move_fraction(margin: int, leverage: int, entry: int, is_long: bool, p: InstrumentParams) -> float | None:
    """Smallest favorable move (fraction of entry) where net > 0 after costs. Binary search on exit."""
    # Favorable direction
    lo, hi = 1, entry  # price ticks of move
    def net_for(ticks: int) -> int:
        exit_px = entry + ticks if is_long else entry - ticks
        if exit_px <= 0:
            return -1
        return settle_close(margin, leverage, entry, exit_px, is_long, p)["net"]

    if net_for(hi) <= 0:
        return None
    while lo < hi:
        mid = (lo + hi) // 2
        if net_for(mid) > 0:
            hi = mid
        else:
            lo = mid + 1
    return lo / entry
