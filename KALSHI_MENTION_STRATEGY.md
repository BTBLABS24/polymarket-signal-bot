# Kalshi Mention Market Strategy — Full Documentation

## Strategy Overview

**Edge:** "What will X say during Y?" markets systematically overprice YES outcomes. People expect mentions that don't happen. We BUY NO cheaply and collect when the word isn't said.

**Action:** BUY NO on mention markets, 0-1h before event starts.

---

## Filters

| Parameter | Value |
|---|---|
| Side | BUY NO |
| NO price range | 5c - 30c |
| Entry window | 0-1h before event start (1.5h upper bound as 30min buffer for 5-min scan cycle) |
| Bet size (current) | $2/bet flat |
| Excluded categories | NBA, NFL, Fight/UFC, Press Briefings, Earnings, MLB, NHL, CFB, MLS, WNBA, Boxing |
| Included categories | Trump events, NCAA basketball, Governor speeches, political hearings, misc |

---

## Backtest Results (Last 3 Months: Nov 2025 - Feb 2026)

### Headline Numbers
- **1,506 markets**, 28.4% win rate
- At $25/bet flat: **+$11,871 PnL, +36.8% ROI, $129/day**
- At $50/bet flat: **+$20,681 PnL, +35.5% ROI, $225/day**
- At $100/bet flat: **+$32,418 PnL, +32.3% ROI, $352/day**

### Scaling Analysis (bet = min(liquidity, cap), no floor)

| Cap | $/week | $/month | ROI |
|---|---|---|---|
| $25 | $903 | $3,871 | +36.8% |
| $50 | $1,574 | $6,744 | +35.5% |
| $75 | $2,071 | $8,877 | +33.8% |
| $100 | $2,467 | $10,571 | +32.3% |
| $150 | $3,166 | $13,569 | +31.2% |
| $200 | $3,731 | $15,991 | +30.4% |
| $500 | $6,123 | $26,243 | +31.1% |

### Monthly Breakdown (cap $100)

| Month | Markets | WR | PnL | ROI | $/day |
|---|---|---|---|---|---|
| 2025-11 | 111 | 26.1% | +$960 | +14.4% | $120 |
| 2025-12 | 373 | 23.3% | +$8,786 | +33.5% | $303 |
| 2026-01 | 643 | 30.8% | +$16,897 | +36.6% | $603 |
| 2026-02 | 379 | 29.8% | +$5,776 | +27.2% | $321 |

### Edge Trend (Monthly ROI, all-time)
Edge is **increasing** over time — more mention series being added by Kalshi:
- Mar 2025: -39%
- Jun 2025: +28%
- Sep 2025: +52%
- Dec 2025: +68%
- Jan 2026: +109%
- Rolling 3-month ROI trending up: 59% → 68% → 79%

### Market Liquidity Distribution
Median market has **$72** of NO-side volume in the 1h pre-event window (NO 5-30c).

| Bet size | % of markets fillable |
|---|---|
| $2 | 94% |
| $25 | 72% |
| $50 | 59% |
| $100 | 42% |
| $200 | 25% |

~118 markets/week in recent months.

---

## Live Bot Results (as of 2026-02-20)

### Bot Trades (settled, under $3 cost = bot strategy only)
- **18 settled trades**, 10W/8L, **55.6% WR**
- **PnL: +$6.82, +25.1% ROI**
- Running at $2/bet flat

### Weather Trades (manual, NOT bot strategy)
- 42 settled, 2W/40L, **-$462.74, -93% ROI**
- These are manual bets, not the bot strategy. Weather markets are efficiently priced.

### Account
- Balance: ~$74.77 (as of last log pull)
- 104 open positions total (mix of bot + manual)

---

## Deployment

### Railway
- **Project:** `attractive-courage` (ID: `778ad27c-ba00-4aa2-96ba-ae31db2155d8`)
- **Service:** `polymarket-signal-bot` (ID: `404236e1-9e40-45c8-8b98-229770d34bed`)
- **Environment:** `production`
- **DRY_RUN:** `true` in env (artifact — bot IS live, placing real orders)

### Environment Variables
| Var | Status |
|---|---|
| `KALSHI_API_KEY_ID` | SET |
| `KALSHI_PRIVATE_KEY_B64` | SET (2237 chars, RSA key) |
| `TELEGRAM_BOT_TOKEN` | SET |
| `TELEGRAM_CHAT_ID` | SET |
| `DRY_RUN` | `true` |

### Key Files
| File | Purpose |
|---|---|
| `realtime_scanner/kalshi_reversion_scanner.py` | Main bot — both strategies (reversion + mention) |
| `mention_markets_cache.json` | Cached market metadata for backtests |
| `mention_trade_cache.json` | Cached trade data for backtests |
| `mention_milestones_cache.json` | Event start times for backtests |
| `backtest_entry_timing.py` | Entry timing backtest (1h vs 3h pre-event) |
| `backtest_event_velocity_strat.py` | Event velocity gating backtest |

### Bot Architecture
- Scans every **5 minutes**
- **Strategy 1 (Reversion):** Fade retail surges, 24h hold
- **Strategy 2 (Mention):** BUY NO 5-30c, $2/bet, 1h pre-event, ex NBA/Fight/Press
- Limit orders rest on book, canceled after **10 minutes** if unfilled
- Auth: RSA-PSS signing with SHA256 (lines 272-290 of scanner)

### Railway CLI Commands
```bash
# Login
railway login

# Link project (one-time)
railway link -p 778ad27c-ba00-4aa2-96ba-ae31db2155d8

# Set service
railway service polymarket-signal-bot

# Pull logs
railway logs -n 100

# Get env vars
railway variables --json

# Deploy (push to Railway)
railway up
```

### Kalshi API Endpoints Used
```
GET /trade-api/v2/portfolio/fills        — trade history
GET /trade-api/v2/portfolio/settlements  — settled positions
GET /trade-api/v2/portfolio/positions    — open positions
GET /trade-api/v2/portfolio/balance      — account balance
```

---

## Key Findings & Decisions

### Why BUY NO works on mention markets
- People overestimate that specific words will be said during events
- Cheap NO (5-30c) means YES is priced 70-95c — very overconfident
- 28% of the time YES hits, but you only pay 5-30c for NO, so the math works

### Why 1h pre-event (not 3h)
- Backtest showed 1h pre-event has highest ROI (+127% at $1/bet over 21 days)
- Earlier entry means more time for price to move against you
- Most volume and price discovery happens in the hour before the event

### Why exclude NBA/NFL/Fight/Press/Earnings
- Sports mention markets have different dynamics (commentators say everything)
- Press briefings are highly scripted — reporters ask predictable questions
- These categories had negative or flat ROI in backtests

### Category performance
- **Trump:** Highest ROI, but high-liquidity Trump markets (above median) have lower WR (17%) vs below-median (55.6%). At flat bet sizes this still works because winners and losers get equal weight.
- **NCAA:** Largest volume of markets, solid positive ROI
- **Governor/Political:** Positive ROI, lower volume

### Liquidity-scaled sizing
- At flat $2, every market gets equal weight — optimal for ROI
- Scaling to liquidity over-allocates to efficiently-priced markets (more liquidity = more efficiently priced = lower edge per dollar)
- But total PnL increases because you're deploying more capital
- Marginal ROI drops from 37% (first $25) to ~27% ($100-200 layer) — still positive

### Conservative income estimate
- **$50 cap:** ~$6,700/month backtest, ~$4,000-4,700 after 30-40% friction haircut
- **$75 cap:** ~$8,900/month backtest, ~$5,300+ after haircut
- **$5k/month is achievable at $75 cap** with comfortable margin for slow months

### Polymarket
- Does NOT have mention markets — this strategy is Kalshi-specific

---

## Scaling Plan
Bot is LIVE at $2/bet. Gradual scaling based on settled trade count:

| Milestone | Bet Size | Capital Needed | Expected $/month |
|---|---|---|---|
| Now (18 settled) | $2/bet | $250 | ~$550 |
| 100 settled (~end of Feb) | $5/bet | $625 | ~$1,350 |
| 200+ settled | $10/bet | $1,250 | ~$2,700 |
| Confidence established | $25-50/bet | $3,100-6,200 | ~$5,700-10,000 |

Statistical significance: need ~105 trades for 95% confidence.
Current rate: ~24 trades/day = 170/week (1.5x higher than backtest assumed).

Capital math: ~96 concurrent positions (24/day x 4-day avg hold) x bet size x 1.3 buffer.

## Next Steps
1. ~~Turn off DRY_RUN to go live~~ — DONE (live at $2/bet)
2. Hit 100 settled trades → bump to $5/bet, deposit to ~$625
3. Validate at $5 → bump to $10/bet, deposit to ~$1,250
4. Optionally implement liquidity-scaled sizing (bet more on liquid markets, less on thin ones)
5. Monitor live fill rates — backtest assumes 100% fill, reality will be lower
6. Track Strategy 1 (reversion) performance once enough trades settle
7. Find additional signal types to stack toward $25k/month target
8. Explore market making on mention markets (top #3 mention trader made $33k/week)
