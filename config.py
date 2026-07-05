"""
POLYBOT — Configuration
All settings in one place. Edit this file to customize.
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Wallet (from .env file) ──────────────────────────────
    PRIVATE_KEY     = os.getenv("PRIVATE_KEY", "")
    # FUNDER_ADDRESS meaning depends on WALLET_SIGNATURE_TYPE:
    #   Type 0 (EOA):              not used, can be blank
    #   Type 1 (POLY_PROXY):       your Polymarket proxy wallet address
    #   Type 2 (POLY_GNOSIS_SAFE): your Polymarket Safe-proxy address
    #   Type 3 (POLY_1271):        your Polymarket deposit wallet address
    # All three (1/2/3) are found on your Polymarket profile page,
    # not in your browser wallet extension. See .env.example.
    FUNDER_ADDRESS  = os.getenv("FUNDER_ADDRESS", "")
    CHAIN_ID        = 137  # Polygon mainnet

    # Signature type — MUST match how you actually use Polymarket:
    #   0 = EOA             — raw wallet private key, trading directly
    #                         (uncommon for normal Polymarket users)
    #   1 = POLY_PROXY      — Email / Magic Link login
    #                         (most common — use this if unsure)
    #   2 = POLY_GNOSIS_SAFE — Browser wallet connected via Polymarket's
    #                         "Connect Wallet" (MetaMask, Coinbase, Rabby)
    #   3 = POLY_1271       — newer deposit-wallet flow. NOT RECOMMENDED
    #                         right now: confirmed open SDK bug as of
    #                         June 2026 (py-clob-client-v2 issues #70,
    #                         #75) rejects every order regardless of
    #                         correct setup. Use type 2 as a working
    #                         alternative if your account uses this flow.
    WALLET_SIGNATURE_TYPE = int(os.getenv("WALLET_SIGNATURE_TYPE", "1"))

    # ── Capital Settings ─────────────────────────────────────
    CAPITAL_MODE      = "FIXED"   # "FIXED" or "AUTONOMOUS"
    STARTING_BALANCE  = 100.0     # Your starting USDC balance
    UNIT_SIZE         = 1.0       # $ per side per pair (FIXED mode)

    # Timeframe-weighted capital allocation. Added after analyzing
    # real 1.5-day trade history from an active wallet: 5-minute
    # markets showed a HIGHER return (10.69%) but LOWER win rate
    # (50.5%) — many small losses offset by fewer larger wins —
    # while 15-minute markets showed a somewhat LOWER return (8.27%)
    # at a meaningfully HIGHER win rate (62.9%) — steadier, less
    # variance. Rather than treating 5min/15min as a binary either/or
    # (the DURATION_MODE toggle above still does that, for testing),
    # this lets capital be weighted differently between them when
    # BOTH are live, so you can deliberately tilt toward steadier
    # 15-min performance, higher-return-but-noisier 5-min, or an
    # even split (the default) — a real allocation decision, not
    # just a mode switch. This ONLY affects FIXED-mode sizing, via
    # CapitalEngine._get_fixed_size().
    #   Values are relative weights, NOT percentages — they're
    #   normalized internally, so {5MIN: 1.0, 15MIN: 1.0} and
    #   {5MIN: 2.0, 15MIN: 2.0} both mean "even split."
    # NOTE: this is based on 1.5 days from ONE wallet — a real but
    # thin sample. Treat the default (even split) as the safe
    # starting point; only tilt this once you have your OWN
    # multi-day results to justify it, per this bot's own trade
    # journal duration-comparison stats.
    # Applies to sizing in BOTH capital modes (FIXED and AUTONOMOUS)
    # — see CapitalEngine.get_size() / _timeframe_multiplier().
    TIMEFRAME_WEIGHT = {
        "5MIN":  1.0,
        "15MIN": 1.0,
    }

    # ── Edge / Profit Thresholds ─────────────────────────────
    # Combined YES+NO cost must be AT OR BELOW these values
    MIN_EDGE_ACTIVE   = 0.95   # >60s remaining
    MIN_EDGE_CAUTIOUS = 0.94   # 30–60s remaining
    MIN_EDGE_FINAL    = 0.93   # 10–30s remaining
    MIN_NET_PROFIT    = 0.03   # $0.03 minimum net per $1 trade

    # ── Expiry / Timing ──────────────────────────────────────
    # IMPORTANT: py_clob_client_v2 order signing takes ~1 second
    # per order in current benchmarks (confirmed against official
    # SDK characteristics, not network latency). A two-leg trade
    # is therefore realistically ~2-3 seconds end to end, not
    # milliseconds. The 10s cutoff below leaves a real margin for
    # this, but don't push it lower without accounting for signing
    # time — see README "Execution Latency" section.
    #
    # Three genuinely distinct stages as expiry approaches:
    #   ACTIVE   (>30s left)   — normal edge threshold
    #   CAUTIOUS (20-30s left) — slightly tighter, reduced size
    #   FINAL    (10-20s left) — tightest edge, smallest size
    #   CLOSED   (<10s left)   — no new trades at all
    # NOTE: FINAL_SECONDS must be strictly between CUTOFF_SECONDS
    # and CAUTIOUS_SECONDS or the FINAL stage becomes mathematically
    # unreachable (confirmed as a real bug: it was previously set
    # equal to CUTOFF_SECONDS, which meant MIN_EDGE_FINAL — the
    # tightest, most conservative threshold — was silently dead
    # code and every trade in the last 30 seconds used the same
    # CAUTIOUS threshold regardless of how close to expiry it was).
    CUTOFF_SECONDS   = 10   # HARD stop: no new trades within 10s
    FINAL_SECONDS    = 20   # Tightest edge only within 10–20s
    CAUTIOUS_SECONDS = 30   # Reduced size within 20–30s
    HIT_COOLDOWN     = 0.5  # Seconds between hits on same market

    # ── Markets to Trade ─────────────────────────────────────
    # 6 assets × 2 durations = 12 pairs total
    ACTIVE_PAIRS = [
        "BTC_5MIN",  "BTC_15MIN",
        "ETH_5MIN",  "ETH_15MIN",
        "XRP_5MIN",  "XRP_15MIN",
        "SOL_5MIN",  "SOL_15MIN",
        "BNB_5MIN",  "BNB_15MIN",
        "DOGE_5MIN", "DOGE_15MIN",
    ]

    # Duration toggle — which market durations are ACTUALLY live-
    # traded right now. This is the default on startup; it can be
    # changed live from the dashboard Settings tab without
    # restarting the bot (see data/runtime_settings.py). Options:
    #   "BOTH"   — trade both 5min and 15min (default, full scale)
    #   "5MIN"   — only 5-minute markets
    #   "15MIN"  — only 15-minute markets
    # This does NOT change ACTIVE_PAIRS or SLUG_PATTERNS above —
    # discovery still tracks all 12 pairs so both durations keep
    # collecting comparison stats in the journal even when only one
    # is actually being traded live. That's what makes the "test
    # both, then scale into the winner" workflow possible: you're
    # never flying blind on the one you've toggled off.
    DURATION_MODE = "BOTH"

    # Slug prefix patterns (fixed — never change)
    SLUG_PATTERNS = {
        "BTC_5MIN":   "btc-up-or-down-5m-",
        "BTC_15MIN":  "btc-up-or-down-15m-",
        "ETH_5MIN":   "eth-up-or-down-5m-",
        "ETH_15MIN":  "eth-up-or-down-15m-",
        "XRP_5MIN":   "xrp-up-or-down-5m-",
        "XRP_15MIN":  "xrp-up-or-down-15m-",
        "SOL_5MIN":   "sol-up-or-down-5m-",
        "SOL_15MIN":  "sol-up-or-down-15m-",
        "BNB_5MIN":   "bnb-up-or-down-5m-",
        "BNB_15MIN":  "bnb-up-or-down-15m-",
        "DOGE_5MIN":  "doge-up-or-down-5m-",
        "DOGE_15MIN": "doge-up-or-down-15m-",
    }

    # ── Risk / Circuit Breakers ──────────────────────────────
    MAX_DAILY_LOSS          = -20.0  # Stop if down $20 today
    MAX_BALANCE_DROP_PCT    = 0.20   # Stop if wallet drops 20%
    MAX_CONSECUTIVE_MISSES  = 10     # 10 FOK failures = pause
    MAX_API_ERRORS          = 5      # 5 API errors = pause
    MAX_HITS_PER_MARKET     = 50     # Max hits per market cycle
    MAX_COST_PER_MARKET     = 50.0   # Max $ deployed per market
    MAX_UNHEDGED_EXPOSURE   = 5.0    # Max unhedged SHARES per single market
    MAX_TOTAL_UNHEDGED_EXPOSURE = 15.0  # Max unhedged SHARES across
                                          # ALL open positions combined —
                                          # catches the case where several
                                          # markets are each near their own
                                          # per-market cap simultaneously

    # Max consecutive same-side unhedged directional holds allowed
    # on a single market before forcing a hedge-or-cut decision.
    # Added after analyzing real trade history from an active
    # wallet: a specific, real market showed five consecutive
    # same-side ("Up") entries at falling prices (0.40→0.43→0.31→
    # 0.31→0.21) with no intervening hedge, which resolved against
    # the position for a $6.52 loss on that single market. This cap
    # forces _handle_one_leg (websocket_listener.py) to stop
    # blindly re-holding the same losing side and instead force a
    # cut or genuine hedge attempt after N same-side entries.
    MAX_SAME_SIDE_STREAK = 3

    # ── Fees (Crypto category on Polymarket) ─────────────────
    CRYPTO_TAKER_RATE = 0.018   # 1.8% peak at $0.50
    POLYGON_GAS       = 0.003   # ~$0.003 per tx
    SLIPPAGE_PER_LEG  = 0.004   # ~0.4¢ slippage per leg

    # ── Scheduler Intervals ──────────────────────────────────
    DISCOVERY_INTERVAL_SECONDS  = 10   # Market discovery
    WALLET_SYNC_INTERVAL_SECONDS = 60  # Wallet balance sync
    RECONCILE_INTERVAL_MINUTES   = 10  # Position reconciliation
    HEALTH_CHECK_INTERVAL_SECONDS = 30 # WebSocket health check

    # ── APIs ─────────────────────────────────────────────────
    GAMMA_API = "https://gamma-api.polymarket.com"
    CLOB_API  = "https://clob.polymarket.com"
    DATA_API  = "https://data-api.polymarket.com"  # Actual held positions —
                                                      # NOT the same as the
                                                      # CLOB's order endpoints,
                                                      # which only show resting
                                                      # limit orders (this bot
                                                      # only uses FOK/FAK,
                                                      # which never rest).
    WS_URL    = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # ── Dashboard ────────────────────────────────────────────
    DASHBOARD_HOST = "0.0.0.0"
    DASHBOARD_PORT = 5000
