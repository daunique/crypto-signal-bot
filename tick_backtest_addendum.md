# Tick-Level Backtest Addendum (max 10-tick duration)

Used the raw 2-second tick stream for R_25 (8.55M ticks, 199 days). Rebuilt the whole indicator/signal/simulation stack at tick resolution (no OHLC): tick-EMA, tick-RSI, ROC, rolling tick-volatility, stochastic, MACD, up/down-run patterns, EMA-pullback — same category coverage as before, adapted for a single price stream. Contract mechanics: entry price at tick *i*, barrier = entry ± (rolling tick std × barrier fraction), duration = 1–10 ticks (searched), settlement = price at tick *i+duration* vs. barrier.

**Method:** searched ~2,000 configs × 6 durations × 4 barrier fractions (~50,000 backtests) on a 50-day tuning window, then validated the two best candidates out-of-sample on the full 199-day dataset.

## Result

Same conclusion as the candle-level backtest, and the tick data makes the mechanism even clearer:

| Duration | Barrier | Overall win rate | Min daily | Max daily | Worst loss streak |
|---|---|---|---|---|---|
| 10 ticks | 0.00 (hypothetical, no barrier) | 50.7% | 32.3% | 67.6% | 11 |
| 10 ticks | 0.05 | 50.0% | 30.8% | 67.6% | 12 |
| 10 ticks | 0.10 | 49.3% | 30.8% | 66.2% | 12 |
| 10 ticks | 0.25 | 47.3% | 27.7% | 64.4% | 12 |

(1-tick duration, tested separately at huge sample sizes, converges to almost exactly 50.0% — as clean a confirmation of "no exploitable edge, fixed-volatility random walk" as you'll get.)

**0 of ~50,000 evaluated configurations met your target** (≥52% every day, ≥50 signals/day, max loss streak ≤2). The subset search briefly surfaced a config that looked like it cleared 52% overall — but that was overfitting to the 50-day tuning window; on the full 199-day out-of-sample set it fell back to ~50.7%, in line with everything else.

Two more things worth naming plainly:
- **Daily win rate swings 30–68% at this trade volume even for the best-found config** — that's the expected statistical noise band around a ~50% true rate at 50-70 trades/day, not something a better filter set removes.
- Shortening duration doesn't help — accuracy gets *closer* to exactly 50% as duration shrinks (1-tick ≈ 50.0%), which is the opposite of what you'd see if there were real short-term momentum to exploit.

This reinforces rather than changes the earlier finding: it's the underlying process (synthetic index, no real serial correlation) plus the barrier structure, not a lack of search coverage. The same recommendations from the first report apply.

## Files added
- `tick_engine.py` — tick-level indicator library, signal generator, barrier-aligned simulator with configurable tick duration (1–10).
- `tick_search.py` — random search harness across duration/barrier/filter space (run: `python3 tick_search.py <n_trials> <n_days_subset_flag> <seed>`).
- `validate_full.py` — re-tests specific candidate configs on the full 199-day tick set to check for overfitting.
