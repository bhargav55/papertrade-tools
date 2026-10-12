# Papertrade tools

Local read-only web app with:

1. **Haircut-aware trade calculator** — bust/ZE prices, breakeven move, payout table, equity at bust.
2. **LP / queue-risk monitor** — polls ~15s, sqlite history, stress table, alerts.

Uses the exact settlement math verified against on-chain closes (deadband `entry//50000`, impact haircut, 2% win fee). Live instrument params from `GET https://exchange.papertrade.xyz/state/trading` (fallback to docs constants). Live mids from Hyperliquid public `info` API.

## Run

```bash
cd /workspace/papertrade/tools
source ../.venv/bin/activate   # or: python3 -m venv .venv && pip install flask httpx pandas pyarrow pytest
python app.py                  # listens on 0.0.0.0:8787
```

Background:

```bash
cd /workspace/papertrade/tools
source ../.venv/bin/activate
nohup python app.py > data/server.log 2>&1 &
```

Open **http://127.0.0.1:8787/**

## API

| Method | Path | Notes |
|---|---|---|
| GET/POST | `/api/calc?asset=BTC&direction=long&margin=100&leverage=100&entry=` | `entry` optional (live mid) |
| GET | `/api/monitor/latest` | latest LP/queue/house sample |
| POST | `/api/monitor/sample` | force a sample now |
| GET | `/api/monitor/history?limit=200` | sqlite history |
| GET | `/api/alerts` | alert JSON |
| GET | `/api/mids` | live BTC/ETH BBO mid |
| GET | `/api/health` | liveness |

Alert rule: `headroom < 25% of LP` or `queue debt > 0` → append `data/alerts.log` + `/api/alerts`.

## Tests

```bash
cd /workspace/papertrade/tools
source ../.venv/bin/activate
pytest -q tests/test_settlement.py
```

Requires `../raw_logs.jsonl` from the earlier indexer for the exact replay test.

## Notes / limits

- Open-house mark prefers the site leaderboard open set; if unavailable, falls back to still-open rows from the local parquet indexer (may be stale vs live ~1.5k opens).
- Site “House PnL” is also shown from `/state/house/current` for sanity.
- Read-only: no intents, no transactions.

## Discrepancies / caveats (live)

- Site open-house PnL is taken from `/state/house/current` `head[2]` (same figure as the House page). Earlier in the day it was about **−$1.37M**; at tool bring-up it was about **+$0.69M** — the book flipped with price.
- Stress ±2% is **clamped to the site delta API range (±1%)**; rows are labeled.
- Optional `local_mark` marks still-open rows from the local indexer parquet (stale subset) for comparison only — do not treat as live.
- Headroom = `LP − max(0, −house_pnl) − queue`. When open house PnL ≥ 0, headroom equals LP (100%).

## Proof curls

```bash
curl -s http://127.0.0.1:8787/api/health
curl -s http://127.0.0.1:8787/api/mids
curl -s 'http://127.0.0.1:8787/api/calc?asset=BTC&direction=long&margin=100&leverage=1000'
curl -s -X POST http://127.0.0.1:8787/api/monitor/sample
curl -s http://127.0.0.1:8787/api/monitor/history?limit=5
curl -s http://127.0.0.1:8787/api/alerts
```


## Railway

- Start: `gunicorn -w 1 -b 0.0.0.0:$PORT --timeout 120 app:app` (see `Procfile` / `railway.json` / `Dockerfile`)
- Persist sqlite: mount a volume at `/data` and set `DATA_DIR=/data`
- Healthcheck: `GET /api/health`
