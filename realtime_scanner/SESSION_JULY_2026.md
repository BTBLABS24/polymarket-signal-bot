# Session Log — July 2026

Summary of the analysis and code changes made to the Kalshi trading bot in this
session, with enough detail to reproduce every step. All changes are on git
branch `kalshi-bot` (remote: `github.com/BTBLABS24/polymarket-signal-bot`).

---

## 1. Environment / how the bot runs

- **Bot file:** `realtime_scanner/kalshi_reversion_scanner.py` (deployed on Railway).
- **Railway project:** display name `Kalshi_Bot_LIVE`, linked dir name `attractive-courage`,
  service `polymarket-signal-bot`.
- **Credentials:** Kalshi API keys live as Railway env vars
  (`KALSHI_API_KEY_ID` + one of `KALSHI_PRIVATE_KEY` / `KALSHI_PRIVATE_KEY_PATH`
  / `KALSHI_PRIVATE_KEY_B64`). They are **not** stored locally, so local runs are
  signal-only ("Auth: MISSING (signal-only mode)"). The bot only trades live from
  the Railway deployment.
- **Deploy flow:** push to `origin/kalshi-bot` → Railway auto-redeploys.
- **Mode:** `MAKER_ONLY = True` — bot only places resting NO-buy limit orders
  (`post_only`), never taker (except the Truth-Social force_taker carve-out, which
  is currently alert-only).

### Pulling live account data (read-only)

```bash
railway login                 # interactive; must be run by the operator
railway run python3 realtime_scanner/analyze_live_kalshi.py
```

This dumps balance / positions / fills / settlements to
`realtime_scanner/live_kalshi_dump.json`. It issues **GET-only** requests (never
places/cancels orders), reusing the bot's RSA-PSS auth scheme.

> **CAUTION — schema bug:** `analyze_live_kalshi.py` has an outdated field
> mapping. The real Kalshi v2 schema uses `count_fp`, `position_fp`, `*_dollars`
> cost fields, and `revenue` in **cents**. Analysis in this session was done via
> inline Python on `live_kalshi_dump.json`, not the script's built-in summary.

---

## 2. Account findings (as of this session)

- Balance ≈ **$63.32**; 763 fills; 3,099 settlements.
- **Lifetime realized PnL ≈ -$4,562**, win rate ~30.5%.
- "Clean" trades only (CLAUDE.md rule: cost < $4 AND NO price 5-30c) ≈ **+$431**.
- **Trump NO-only strategy IS profitable:** ≈ +$129 over the last 3 weeks
  (WR 36%, avg +$1.01/market); +$236 on the clean subset.
- Losses concentrated in **both-sides holdings** and **oversized YES** anomalies
  (e.g. JUL07-AI -$89, JUL07-DEMO -$72, JUL06-AMERF -$37 on 3,500 contracts),
  not the core NO strategy.

Kalshi fill quirk to remember: a "sell no @ 0.26" fill **acquires** a NO position
at 0.26 cost (v2 YES-centric single book). Settlement data is ground truth for PnL.

---

## 3. Change 1 — Bump Trump NO maker strat to $6/bet

**Commit `48fa979`** — "Bump Trump NO maker strat to $6/bet".

A true $6 required lifting THREE interacting $4 chokepoints, all scoped to Trump
only (`get_mention_category(ticker) == 'Trump'`, matches `KXTRUMPMENTION` /
`KXTRUMPMENTIONB` / `TRUMPSAY` tickers). Everything else stays capped at $4.

Edits in `kalshi_reversion_scanner.py`:

1. **`CATEGORY_BET_OVERRIDE`** (~line 86): added `'Trump': 6`.
2. **New constants** (near line 100):
   ```python
   TRUMP_MAKER_BET_DOLLARS = 6   # Trump NO maker size & per-order cap
   TRUMP_MAX_MARKET_DOLLARS = 6  # per-ticker ceiling for Trump maker NO
   ```
3. **`create_order` per-order cap** (~line 1319): made Trump-aware:
   ```python
   per_order_cap = TRUMP_MAKER_BET_DOLLARS if get_mention_category(ticker) == 'Trump' else MAX_TRADE_DOLLARS
   if count * price_cents / 100.0 > per_order_cap + 1e-9: ... return None
   ```
4. **Narrow-spread clamp** (~line 6245): Trump can reach $6 on `<10c` spreads;
   others stay $5.
5. **Per-market cap** (~line 6262): `if maker_cat_name == 'Trump': maker_market_cap = max(maker_market_cap, TRUMP_MAX_MARKET_DOLLARS)`.

Unchanged: `MAX_MAKER_NO_PRICE_CENTS = 30`, all non-Trump caps.

**Risk note:** balance is ~$63; at $6/bet the aggregate caps
(`MENTION_MAX_EVENT_DOLLARS = 50`, `PREMARKET_MAX_TOTAL_RESTING_DOLLARS = 60`)
are the main backstop against a run of Trump fills consuming the account.

---

## 4. Change 2 — Paper-trade tracking for the Truth-Social YES strat

**Commit `7817928`** — "Add paper-trade tracking for alert-only Truth-Social YES strat".

### The strategy (`TRUTH_YES_*`, config ~line 538)
Thesis: when Trump posts a word on Truth Social within `TRUTH_YES_WINDOW_H = 24`h
before one of his mention events, and YES is still ≤ `TRUTH_YES_MAX_CENTS = 30`c,
buy YES ($5/trade) — the post makes him more likely to say it.

- `TRUTH_YES_ENABLED = True`, `TRUTH_YES_ALERT_ONLY = True` — detects signals and
  fires a Telegram alert but does **not** place an order. Set alert-only in commit
  `212cc10`.

### What was added
A fully isolated paper book so we get real would-be PnL with zero risk:

1. **Parametrized `KalshiPositionTracker`** to accept `positions_file` / `csv_file`
   (defaults = live files). Live tracker unchanged.
2. **`add(..., is_paper=True)`** → marks `is_live=False`, `is_paper=True`.
3. **New state files** (in the deployment dir):
   - `truth_yes_paper_positions.json` (open/closed book)
   - `truth_yes_paper_history.csv` (per-trade log)
   - Constants: `PAPER_TRUTH_POSITIONS_FILE`, `PAPER_TRUTH_CSV`.
4. **`self.paper_truth = KalshiPositionTracker(PAPER_TRUTH_POSITIONS_FILE, PAPER_TRUTH_CSV)`**
   in scanner `__init__`.
5. **`_record_paper_truth_yes(sig)`** — records a simulated fill at the **YES ask
   reported in the alert** (`sig['yes_ask_cents']`, the taker price it would have
   paid), $5/trade, ≤30c. Restart-safe dedup (skips if a paper position for that
   ticker already exists, open or closed). Called in the alert-only branch after
   the Telegram alert.
6. **Settlement** — `self.paper_truth.check(self.client)` runs each scan cycle,
   right after the live `check()`. Prints `PAPER SETTLED (WIN/LOSS) ... would-be
   P&L`, logs `truth_yes_paper_settled`. Reuses existing PnL math
   (`truth_cheap_yes` is treated as a YES-buy):
   - word said (`result=yes`) → `+fill_count × (1 − fill_price)`
   - word not said (`result=no`) → `−fill_count × fill_price`

### Reviewing paper results later
Pull the two paper files via `railway run` (read-only) and summarize win rate /
total would-be PnL after a few days of signals. Note: no historical backfill —
alert-only mode never persisted anything, so tracking starts from the next signal.

---

## 5. Strategy / risk config reference

- **`MENTION_MAKER_SERIES`** (~line 309) — series allowed to rest a maker NO bid:
  `KXTRUMPMENTION`, `KXTRUMPMENTIONB`, `KXVANCEMENTION`, `KXHEARINGMENTION`
  (+ prefix `KXEARNINGSMENTION`).
  - HEARING added at $2/bet on a +73.6% backtest (151 fills). Vance/Earnings adds
    were "maker untested live" (their live losses were on the dead taker path).
  - ⚠️ The comment above the list still says "Restricted to Trump only" — that is
    **stale**; commits `f3ba9af` / `8ac8de8` re-added Vance/Hearing/Earnings.
- **`CATEGORY_BET_OVERRIDE`** (~line 86): `VANCE:2, Earnings:2, HEARING:2, Trump:6`.
- **Hard caps:** `MAX_TRADE_DOLLARS=4` ($6 for Trump), `MAX_MAKER_NO_PRICE_CENTS=30`,
  `GLOBAL_MAX_MARKET_DOLLARS=4`, `PREMARKET_BET_DOLLARS=4`.
- **`low_balance`** trips only below **$1** (not a concern at $63).
- **CLAUDE.md clean-trade rule:** cost < $4 per trade AND NO price 5-30c.

---

## 6. Commits made this session (branch `kalshi-bot`)

| Commit | Description |
|--------|-------------|
| `48fa979` | Bump Trump NO maker strat to $6/bet |
| `7817928` | Add paper-trade tracking for alert-only Truth-Social YES strat |

Both pushed to `origin/kalshi-bot`; Railway auto-redeployed.
