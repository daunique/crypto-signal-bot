# Deriv Higher/Lower Bot — Backtest Findings

**Data used:** R_25 3-min OHLC (Jan 1 – Jul 18 2026, ~199 days, 95k candles), plus JD25 and stpRNG as cross-checks. Mechanics replicated exactly from your `engine.py`/`strategy.py`/`config.py`: decision at each 3-min candle close → entry = that close → barrier = entry ± ATR(15)×`BARRIER_ATR_FRACTION` (0.25 default) in the predicted direction → HIGHER wins if the *next* candle's close clears the barrier, LOWER wins if it clears below.

## 1. As-shipped strategy, as-is

Running your exact `strategy.py` confluence logic (`min_score=6`) with the real barrier:

| Metric | Value |
|---|---|
| Overall win rate | **35.8%** |
| Daily win rate range | 25.8% – 42.9% |
| Days ≥52% win rate | **0 of 198** |
| Worst loss streak | 19 |
| Signals/day | ~248 |

Every single day falls short of 52%. This isn't a tuning problem — it's structural, confirmed below.

## 2. Why: isolating the barrier from the direction call

Re-running the same signals with the barrier offset set to **zero** (i.e., just "was the raw direction call right?") gives **50.3%** overall — a coin flip, which is exactly what you'd expect from a Deriv synthetic index: these are generated as a fixed-volatility random walk with no real serial correlation, so no combination of EMA/RSI/MACD/etc. has genuine predictive edge beyond noise. The barrier then subtracts a further ~14-15 points because it requires the price to clear entry *plus* an ATR-scaled buffer within one candle, not just land on the right side of entry — that's what drags 50% down to 36%.

## 3. Broad parameter search (per your spec)

I built a full search harness (`engine.py` + `search.py`, included) covering every category you listed — EMA/SMA/MTF trend, ADX, slope, RSI/MACD/Stochastic/Williams %R/ROC momentum, EMA21/EMA50/2-candle/3-candle pullback structures, ATR & volatility-regime filters, and body-ratio/wick/engulfing/momentum-candle/consecutive-candle structure — and ran **~13,000 randomly sampled configurations** across R_25, JD25, and stpRNG, at barrier fractions 0, 0.05, 0.1, 0.15, 0.25.

**Result: 0 configurations, on any instrument, met your target (min daily win rate ≥52%, ≥50 signals/day, max loss streak ≤2).** Not close, either:

- Best *overall* win rate at ≥50 signals/day, **with zero barrier** (the unrealistically favorable case — pure direction-only, not an actual tradeable Higher/Lower contract): **51.3%** (R_25), and even that config's *worst single day* was 28.9%.
- Taking that single best config and applying the real barrier:

| Barrier fraction | Overall win rate | Min daily win rate | % of days <52% |
|---|---|---|---|
| 0.00 (no barrier, hypothetical) | 51.3% | 28.9% | 55% |
| 0.05 | 48.3% | 28.0% | 71% |
| 0.10 | 45.3% | 28.0% | 85% |
| 0.25 (bot default) | 36.3% | 20.3% | 100% |

- No configuration anywhere in the search kept the *worst* day above ~45%, let alone 52% on every day.
- Max loss streaks across the qualifying (≥50 signals/day) configs ran 10–19, not ≤2. A max-loss-streak of 2 across 50+ independent ~50%-probability trades/day is itself extremely unlikely for *any* strategy with a real edge under ~55-60% per-trade accuracy — it would require either a near-70%+ true win rate or artificial trade-gating that would cut volume far below 50/day.

## 4. Honest conclusion

I want to be straightforward rather than hand you numbers that look good but aren't real: **a strategy that hits a 52%+ win-rate floor on every single day, with 50+ signals/day and a max loss streak of 2, is not achievable on this instrument/contract with technical-indicator filtering** — and I don't believe it's achievable at all, for a structural reason rather than a search-coverage one:

- Deriv's synthetic Volatility/Jump/Step indices are algorithmically generated at a fixed, disclosed volatility with no real autocorrelation — confirmed directly above (zero-barrier direction accuracy sits at ~50% regardless of filter combination).
- The Higher/Lower barrier makes the bar *harder* than 50/50 by design (you must clear an offset beyond entry, not just be directionally right), so realistic accuracy is *below* 50%, not above.
- Day-to-day win rate on ~50-trade samples at a true ~50% edge has a standard error of ~7 points — daily win rate swinging between 25% and 60% is expected statistical noise, not a sign the strategy needs more tuning. A hard floor of "never below 52%, every day" is not a property any real trading edge on this process can guarantee at that sample size.

## 5. What I'd actually recommend

- If the goal is a positive-expectancy bot at all: barrier fraction near **0** (i.e., trade Rise/Fall, not Higher/Lower) removes the biggest structural handicap — you're back to needing >50% direction accuracy plus payout odds, not >50%-plus-a-buffer.
- Drop the "every day ≥52%" requirement in favor of a **statistical** target (e.g., "overall win rate ≥52% over a rolling 200+ trade window, tracked with a control chart") — that's testable and defensible; a hard daily floor isn't, on this data.
- Loosen the loss-streak requirement or add a circuit breaker (pause after 2 consecutive losses) rather than expecting the *strategy itself* to structurally prevent a 3rd loss — no confluence-score filter can guarantee that on a near-random process.
- If you want, I can re-run the same search targeting a softer, realistic goal (e.g., best achievable overall win rate at ≥50 signals/day, or best win rate at lower volume) and hand you the actual best config with full daily-by-day numbers, rather than one that doesn't exist.

## Files included
- `engine.py` — data loader, full indicator library, configurable signal generator, vectorized trade simulator (barrier-aligned), metrics.
- `search.py` — random search harness across the full filter space (run: `python3 search.py <path_to_3min_csv> <n_trials>`).
- `baseline.py` — literal replication of your current `strategy.py` for a clean before/after comparison.
