# hl-mm — A Market-Making Bot for Hyperliquid Perpetuals

A small, readable, *honest* market-making bot for the Hyperliquid perp DEX.

## Read this first (no, really)

**Nobody can hand you a "profitable" market-making bot.** Anyone claiming
otherwise is either selling you something or lying. This repo gives you a
solid, well-instrumented framework — profitability depends entirely on:

1. **The market you pick.** Mainline pairs (BTC, ETH, SOL) are dominated by
   pro market makers running co-located infra and tier-3+ fee rebates. You
   will lose to them on those names. You have a much better shot in
   smaller-cap perps where pro MMs are absent or thin — at the cost of more
   volatility and adverse selection risk.
2. **Your fee tier.** At base tier the Hyperliquid maker fee is **+1.5 bp**
   (you pay), not a rebate. Your round-trip cost is ~3 bp before P&L. Your
   spread *must* clear that, or you bleed by definition. Maker rebates only
   start at Tier 2 (>$25M 14-day volume). Plan accordingly.
3. **Your risk discipline.** A market-making bot that doesn't aggressively
   manage inventory and cut losses is a tool for converting capital into
   pnl for whoever is picking you off.

This bot defaults to **paper mode on testnet with $50 quote size**. Keep it
that way until you understand what every dial does.

This is **not** financial advice. Trade at your own risk. You can and likely
will lose money.

## Why Hyperliquid (and not Pump.fun)

The user originally asked "Hyperliquid or Pump.fun, whichever is easiest."
We chose Hyperliquid because:

- It is a real **central limit order book** with a documented API and a
  maintained Python SDK. Two-sided market making is a meaningful activity
  here.
- Pump.fun is a **bonding-curve launchpad** on Solana. There is no order
  book to make markets on — "trading" there is bonding-curve sniping and
  arbitrage, which is a different (and adversarial) business.

## What this bot actually does

A single-symbol, single-pair quoting strategy:

1. Subscribes to the L2 order book and BBO over WebSocket.
2. Computes a **fair price** as the size-weighted micro-price.
3. Computes a **half-spread** as `max(min_half_spread, vol_factor * sigma)`
   where `sigma` is short-window realized vol.
4. Applies an **inventory skew**: shifts quotes against the side of your
   current position to mean-revert inventory toward zero.
5. Posts one bid and one ask sized to `quote_notional_usd`, using
   client order IDs (cloids) for idempotent management.
6. Re-quotes when its target price drifts beyond `requote_threshold_bps`.
7. Runs continuous **risk checks**: hard position cap, session-loss kill
   switch, stale-feed guard, per-minute order rate limit.
8. On shutdown (or kill switch), cancels everything and (if configured)
   flattens position with a market order.

## Project layout

```
mm/
  run.py                     # entry point
  config.example.yaml        # copy to config.yaml and edit
  .env.example               # copy to .env and fill in keys
  requirements.txt
  mm_bot/
    __init__.py
    config.py                # config dataclass + loader
    logging_setup.py
    hl_utils.py              # tick/lot rounding, cloid helpers
    state.py                 # book, position, PnL, rolling vol, history
    quoter.py                # micro-price + spread + inventory skew
    risk.py                  # all kill switches and rate limits
    executor.py              # live executor (real orders)
    paper.py                 # paper-mode executor (simulated fills)
    strategy.py              # main MM loop
    dashboard.py             # web UI (stdlib http.server, no extra deps)
```

## Dashboard

When the bot is running it serves a live web UI at
**http://127.0.0.1:8765** (configurable). Open it in any browser to see:

- Status pill (RUNNING / PAUSED / HALTED), network, mode, symbol, uptime
- KPI cards: mid, spread, realized vol, position size + notional,
  realized / unrealized / total PnL
- Current quote panel: fair price, half-spread, inventory skew, target
  bid/ask vs. live book bid/ask
- Open orders table (side, price, size, notional, cloid/oid)
- Recent fills table (time, side, price, size, realized PnL, position after)
- A live mid + position + realized PnL chart (rolling 10 min)
- Full configuration dump

The page polls `/api/snapshot` once per second. There is **no auth** —
it binds to `127.0.0.1` by default. Don't expose it to the public
internet without putting auth in front.

Disable it by setting `dashboard.enabled: false` in `config.yaml`.

## Quickstart

```bash
# 1. Set up Python environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 2. Configure
cp config.example.yaml config.yaml
cp .env.example .env
# edit .env  -> add HL_PRIVATE_KEY (use an agent wallet, not your main wallet!)
# edit config.yaml -> network: testnet, mode: paper to start

# 3. Run
python run.py
```

You will see one log line per loop iteration with mid, target bid/ask,
position, session PnL, and order state. `Ctrl-C` cleanly cancels and exits.

## Recommended progression

Move through these stages slowly. Don't skip stages.

1. **Paper / testnet** — make sure quotes land where you expect, vol & skew
   behave, kill switches trigger.
2. **Paper / mainnet** — same, against real prices and real adverse
   selection (no money at risk).
3. **Live / testnet, $50 size** — verify signing, order placement, fills,
   cancels, and modify behavior end-to-end.
4. **Live / mainnet, $50 size** — and only after stages 1–3 look correct.
   Increase size only after weeks of stable, positive performance net of
   fees and slippage.

## Generating an agent wallet (recommended)

Don't put your main wallet's private key in `.env`. Instead:

1. In the Hyperliquid UI, go to Sub-Accounts / API and create an **Agent
   Wallet**. You will get a new private key. Approve it.
2. Put that key in `HL_PRIVATE_KEY`.
3. Put your **main wallet address** in `HL_ACCOUNT_ADDRESS`. The agent
   signs orders, but funds and positions belong to your main wallet.

Agent wallets cannot withdraw funds, only trade. If the key leaks, the
attacker can lose your money trading but cannot steal it.

## Tuning notes

- **`min_half_spread_bps`**: must exceed your maker fee. At base tier set
  this to at least `4.0` (so round trip ≥ 8 bp vs ~3 bp fees).
- **`vol_factor`**: higher = wider quotes in volatile periods (fewer fills,
  less adverse selection). Start around 1.5.
- **`inventory_skew_bps`**: the most important knob. Too low and you let
  inventory drift; too high and you give up edge to flatten. Start around
  6 bp at full position cap.
- **Symbol selection**: try a smaller-cap perp where you can actually be
  the best resting bid/ask. If you are constantly the 5th best price, you
  will not get filled and will not earn anything.

## License

MIT. Do whatever you want with it. You assume all risk.
