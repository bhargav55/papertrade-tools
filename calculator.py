"""Haircut-aware trade calculator."""
from __future__ import annotations
from settlement import (
    DEFAULT_PARAMS, InstrumentParams, WAD, to_raw_price, from_raw_price,
    settle_close, bust_price, zero_equity_price, equity_at_price, breakeven_move_fraction,
)
from chain import get_live_params, hl_bbo_mid

MOVE_PCTS = [0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0]

def calculate(
    asset: str,
    direction: str,
    margin_usd: float,
    leverage: int,
    entry_usd: float | None = None,
    params: dict[str, InstrumentParams] | None = None,
) -> dict:
    asset = asset.upper()
    if asset not in ("BTC", "ETH"):
        raise ValueError("asset must be BTC or ETH")
    is_long = direction.lower() in ("long", "l", "buy")
    if leverage < 1 or leverage > 1000:
        raise ValueError("leverage 1..1000")
    if margin_usd <= 0:
        raise ValueError("margin must be > 0")

    params = params or get_live_params()
    p = params[asset]
    bbo = None
    if entry_usd is None:
        bbo = hl_bbo_mid(asset)
        entry_usd = bbo["mid"]

    entry = to_raw_price(entry_usd, p.price_scale)
    margin = int(round(margin_usd * WAD))
    notional = margin_usd * leverage

    bust = bust_price(entry, leverage, is_long, p)
    ze = zero_equity_price(entry, leverage, is_long)
    at_bust = equity_at_price(margin, leverage, entry, bust, is_long, p)
    be = breakeven_move_fraction(margin, leverage, entry, is_long, p)

    rows = []
    for pct in MOVE_PCTS:
        ticks = int(round(entry * (pct / 100.0)))
        if ticks < 1:
            ticks = 1
        exit_px = entry + ticks if is_long else entry - ticks
        if exit_px <= 0:
            continue
        r = settle_close(margin, leverage, entry, exit_px, is_long, p)
        raw_no_db = margin * leverage * ticks // entry  # raw without deadband for "% of raw kept"
        # Prefer deadband-aware raw from settlement as denominator when > 0
        denom = r["raw_pnl"] if r["raw_pnl"] > 0 else raw_no_db
        kept = (r["net"] / denom * 100.0) if denom > 0 else 0.0
        rows.append({
            "move_pct": pct,
            "exit_usd": from_raw_price(exit_px, p.price_scale),
            "raw_pnl_usd": r["raw_pnl"] / WAD,
            "after_haircut_usd": r["pnl_after_haircut"] / WAD,
            "fee_usd": r["fee"] / WAD,
            "net_usd": r["net"] / WAD,
            "pct_of_raw_kept": kept,
            "equity_usd": (margin + r["net"]) / WAD,
        })

    return {
        "asset": asset,
        "direction": "long" if is_long else "short",
        "margin_usd": margin_usd,
        "leverage": leverage,
        "notional_usd": notional,
        "entry_usd": from_raw_price(entry, p.price_scale),
        "entry_raw": entry,
        "bbo": bbo,
        "params": {
            "base_rate": p.base_rate / WAD,
            "rate_multiplier": p.rate_multiplier / WAD,
            "position_multiplier": p.position_multiplier / WAD,
            "reference_notional_usd": p.reference_notional / WAD,
            "bust_buffer_bp": p.bust_buffer / WAD * 1e4,
            "win_fee": p.win_fee_rate / WAD,
            "price_scale": p.price_scale,
        },
        "bust_price_usd": from_raw_price(bust, p.price_scale),
        "zero_equity_price_usd": from_raw_price(ze, p.price_scale),
        "bust_move_bp": abs(bust - entry) / entry * 1e4,
        "zero_equity_move_bp": abs(ze - entry) / entry * 1e4,
        "breakeven_move_pct": None if be is None else be * 100,
        "equity_if_self_close_at_bust_usd": at_bust["equity"] / WAD,
        "pnl_if_self_close_at_bust_usd": at_bust["net"] / WAD,
        "move_table": rows,
    }
