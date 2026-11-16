# LuxAlgo Upstox Multi-Timeframe Bot (3m + 5m + 15m)

Single process that runs **three independent** LuxAlgo strategies on NIFTY options:

| Timeframe | Default SL | Breakeven | Trail start | Giveback |
|-----------|------------|-----------|-------------|----------|
| 3m | 15% | +6% | +8% | 20% |
| 5m | 20% | +8% | +11% | 22% |
| 15m | 25% | +10% | +14% | 25% |

Each timeframe has its own position, stop, and trade counter. One shared spot feed and one warm-up fetch.

> **Note:** This is a local / manual runner. GitHub Actions (if added) is for testing only, not unattended production.

---

## Features

- 3m / 5m / 15m in one process (configurable via `--intervals`)
- Trendline breakouts + support/resistance bounces
- Per-timeframe risk parameters
- Closed-bar signal evaluation
- Stale-bar protection
- Force exit 15:15 IST
- State file: `bot_state_multi.json`

---

## Token (same name as other LuxAlgo repos)

| Priority | Source |
|----------|--------|
| 1 | `UPSTOX_TOKEN` |
| 2 | `UPSTOX_ACCESS_TOKEN` |
| 3 | `--token` |

Add secret **UPSTOX_TOKEN** under:
**Settings → Secrets and variables → Actions** in this repository.

---

## Quick start

```bash
pip install -r requirements.txt
export UPSTOX_TOKEN="your_token_here"

python luxalgo_upstox_multi_tf.py --mode paper
python luxalgo_upstox_multi_tf.py --mode live --qty 65
```

### Useful flags

```bash
# Only 5m and 15m
python luxalgo_upstox_multi_tf.py --intervals 5,15 --mode paper

# Custom SL per TF
python luxalgo_upstox_multi_tf.py --sl 3=12,5=18,15=22 --mode paper
```

---

## Files

| File | Purpose |
|------|---------|
| `luxalgo_upstox_multi_tf.py` | Main bot |
| `bot_state_multi.json` | Positions + trade counts (auto) |
| `luxalgo_live_production_multi.log` | Log |
| `requirements.txt` | Dependencies |
| `.env.example` | Token template |

---

## Disclaimer

Experimental software. Options trading involves substantial risk of loss. Use at your own risk.
