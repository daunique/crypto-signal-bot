from datetime import datetime
from extensions import db


class Signal(db.Model):
    __tablename__ = 'signals'

    id                = db.Column(db.Integer, primary_key=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    symbol            = db.Column(db.String(20), nullable=False)
    timeframe         = db.Column(db.String(4), nullable=False, default='15m')  # '5m' or '15m' —
                                                                                  # default preserves old rows as 15m
    candle_open_time  = db.Column(db.DateTime, nullable=False)
    candle_close_time = db.Column(db.DateTime, nullable=False)
    signal_direction  = db.Column(db.String(4), nullable=False)   # UP or DOWN
    ml_confidence     = db.Column(db.Float)
    rsi_14            = db.Column(db.Float)
    macd_hist         = db.Column(db.Float)
    adx               = db.Column(db.Float)
    vol_ratio         = db.Column(db.Float)
    tier              = db.Column(db.String(10))
    outcome           = db.Column(db.String(10), default='PENDING')
    open_price        = db.Column(db.Float)
    close_price       = db.Column(db.Float)
    mode              = db.Column(db.String(10), default='shadow')
    order_id          = db.Column(db.String(100))
    market_slug       = db.Column(db.String(200))
    condition_id      = db.Column(db.String(100))  # bytes32 hex for /portfolio/redeem
    position_size     = db.Column(db.Float)
    contracts_bought  = db.Column(db.Float)
    contract_price    = db.Column(db.Float)
    telegram_sent     = db.Column(db.Boolean, default=False)
    limitless_fill    = db.Column(db.String(10), default='NEUTRAL')  # FILLED / PARTIAL / UNFILLED / NEUTRAL / SHADOW
    best_entry_pct    = db.Column(db.Float, default=None)
    poly_order_id     = db.Column(db.String(120), default=None) # Polymarket order ID
    poly_fill         = db.Column(db.String(10),  default='NEUTRAL') # FILLED/PARTIAL/UNFILLED/NEUTRAL
    poly_market_slug  = db.Column(db.String(200), default=None) # exact slug traded — resolve must use
    poly_token_id     = db.Column(db.String(100), default=None) # the specific outcome token bought — for dip tracking

    # ── Polymarket data parity (mirrors the Limitless resolution/fill fields above) ──
    poly_position_size    = db.Column(db.Float, default=None)   # intended Polymarket stake at order time
    poly_outcome          = db.Column(db.String(10), default=None)  # WIN/LOSS per Polymarket's OWN resolution
    poly_resolution_source = db.Column(db.String(20), default=None)  # 'POLYMARKET' or 'OKX_FALLBACK'
    poly_fill_ratio        = db.Column(db.Float, default=None)
    poly_filled_usd         = db.Column(db.Float, default=None)

    # ── Platform-native entry/close prices (Chainlink, not OKX) ──
    # sig.open_price/close_price stay OKX-sourced (that's the ML signal's own
    # basis); these are each venue's OWN reference price for its own log.
    limitless_close_price  = db.Column(db.Float, default=None)
    poly_open_price        = db.Column(db.Float, default=None)
    poly_close_price       = db.Column(db.Float, default=None)
    poly_best_entry_pct    = db.Column(db.Float, default=None)  # best GTC dip seen on Polymarket's own book

    # Resolution-source tracking: Limitless winningOutcomeIndex vs OKX candle
    resolution_source     = db.Column(db.String(20), default=None)  # 'LIMITLESS' or 'OKX_FALLBACK'
    okx_outcome           = db.Column(db.String(10), default=None)  # what OKX alone would have said
    limitless_open_price  = db.Column(db.Float, default=None)       # Pyth baseline price Limitless resolves against
    # Fill-amount tracking: GTC limit orders can partial-fill
    fill_ratio            = db.Column(db.Float, default=None)  # filled_usd / intended position_size
    filled_usd            = db.Column(db.Float, default=None)  # actual USDC amount that settled on-chain

    def to_dict(self):
        return {
            'id':                self.id,
            'created_at':        self.created_at.isoformat() if self.created_at else None,
            'symbol':            self.symbol,
            'timeframe':         self.timeframe or '15m',
            'candle_open_time':  self.candle_open_time.isoformat() if self.candle_open_time else None,
            'candle_close_time': self.candle_close_time.isoformat() if self.candle_close_time else None,
            'signal_direction':  self.signal_direction,
            'ml_confidence':     round(self.ml_confidence * 100, 1) if self.ml_confidence else None,
            'rsi_14':            round(self.rsi_14, 2) if self.rsi_14 else None,
            'adx':               round(self.adx, 2) if self.adx else None,
            'vol_ratio':         round(self.vol_ratio, 2) if self.vol_ratio else None,
            'tier':              self.tier,
            'outcome':           self.outcome,
            'open_price':        self.open_price,
            'close_price':       self.close_price,
            'mode':              self.mode,
            'order_id':          self.order_id,
            'market_slug':       self.market_slug,
            'condition_id':      self.condition_id,
            'position_size':     self.position_size,
            'contracts_bought':  self.contracts_bought,
            'contract_price':    self.contract_price,
            'limitless_fill':    self.limitless_fill or 'NEUTRAL',
            'best_entry_pct':    round(self.best_entry_pct, 1) if self.best_entry_pct is not None else None,
            'poly_order_id':     self.poly_order_id,
            'poly_fill':         self.poly_fill or 'NEUTRAL',
            'poly_market_slug':       self.poly_market_slug,
            'poly_token_id':          self.poly_token_id,
            'poly_position_size':     self.poly_position_size,
            'poly_outcome':           self.poly_outcome,
            'poly_resolution_source': self.poly_resolution_source,
            'poly_fill_ratio':        round(self.poly_fill_ratio, 4) if self.poly_fill_ratio is not None else None,
            'poly_filled_usd':        round(self.poly_filled_usd, 4) if self.poly_filled_usd is not None else None,
            'limitless_close_price':  self.limitless_close_price,
            'poly_open_price':        self.poly_open_price,
            'poly_close_price':       self.poly_close_price,
            'poly_best_entry_pct':    round(self.poly_best_entry_pct, 1) if self.poly_best_entry_pct is not None else None,
            'resolution_source':    self.resolution_source,
            'okx_outcome':          self.okx_outcome,
            'limitless_open_price': self.limitless_open_price,
            'fill_ratio':           round(self.fill_ratio, 4) if self.fill_ratio is not None else None,
            'filled_usd':           round(self.filled_usd, 4) if self.filled_usd is not None else None,
        }


class PairLadder(db.Model):
    """
    Independent martingale + breaker state, one row per (symbol, timeframe, venue).

    Replaces the old global singleton fields on Settings (martingale_streak,
    poly_martingale_streak, cooldown_remaining, cooldown_loss_count,
    pair_loss_cooldowns) for the deterministic V2 parallel engine, where many
    pairs across both timeframes can hold independent open positions at once.
    Each (symbol, timeframe, venue) triple gets its own consecutive-loss
    counter and cooldown clock, matching the per-pair breaker validated in
    backtesting (worst-case streak ~3-4 per stream, vs. ~14 for a single
    shared/combined stream across everything).

    consecutive_losses drives BOTH:
      - martingale position sizing (index into Settings.martingale_sequence /
        poly_martingale_sequence for this venue)
      - the breaker: at consecutive_losses >= 3, cooldown_until is set and new
        signals for this (symbol, timeframe, venue) are skipped until now >=
        cooldown_until AND the resuming candidate's magnitude clears the
        rearm threshold (base_threshold * REARM_MULT) — see signal_engine.py.

    A win resets consecutive_losses to 0 and clears cooldown_until.
    Settings.martingale_cap (existing field, e.g. 10) still applies as an
    outer safety-valve hard-reset, in case of an unexpected extreme run.
    """
    __tablename__ = 'pair_ladders'

    id                 = db.Column(db.Integer, primary_key=True)
    symbol             = db.Column(db.String(20), nullable=False)
    timeframe          = db.Column(db.String(4),  nullable=False)  # '5m' or '15m'
    venue              = db.Column(db.String(12), nullable=False)  # 'limitless' or 'polymarket'
    consecutive_losses = db.Column(db.Integer, default=0)
    cooldown_until     = db.Column(db.DateTime, default=None)      # None = not in cooldown
    updated_at         = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('symbol', 'timeframe', 'venue', name='uq_pair_ladder_key'),
    )

    def to_dict(self):
        return {
            'symbol':             self.symbol,
            'timeframe':          self.timeframe,
            'venue':              self.venue,
            'consecutive_losses': self.consecutive_losses or 0,
            'cooldown_until':     self.cooldown_until.isoformat() if self.cooldown_until else None,
            'updated_at':         self.updated_at.isoformat() if self.updated_at else None,
        }


class DailyStats(db.Model):
    __tablename__ = 'daily_stats'

    id            = db.Column(db.Integer, primary_key=True)
    date          = db.Column(db.Date, unique=True, nullable=False)
    total_signals = db.Column(db.Integer, default=0)
    wins          = db.Column(db.Integer, default=0)
    losses        = db.Column(db.Integer, default=0)
    win_rate      = db.Column(db.Float, default=0.0)
    mode          = db.Column(db.String(10), default='shadow')

    def __init__(self, **kwargs):
        # Ensure integer fields are never None on new instances
        # (SQLAlchemy column defaults only apply at DB INSERT, not Python instantiation)
        kwargs.setdefault('total_signals', 0)
        kwargs.setdefault('wins', 0)
        kwargs.setdefault('losses', 0)
        kwargs.setdefault('win_rate', 0.0)
        super().__init__(**kwargs)

    def to_dict(self):
        return {
            'date':          self.date.isoformat(),
            'total_signals': self.total_signals,
            'wins':          self.wins,
            'losses':        self.losses,
            'win_rate':      round(self.win_rate, 2),
            'mode':          self.mode,
        }


class Settings(db.Model):
    __tablename__ = 'settings'

    id                   = db.Column(db.Integer, primary_key=True)
    mode                 = db.Column(db.String(10), default='shadow')
    position_size        = db.Column(db.Float, default=10.0)
    use_martingale       = db.Column(db.Boolean, default=False)
    martingale_sequence  = db.Column(db.String(200), default='1,1.5,2,3,4.5,6.7')  # comma-separated custom stakes
    martingale_step      = db.Column(db.Float, default=0.50)   # legacy — kept for migration safety
    martingale_cap       = db.Column(db.Integer, default=10)   # hard reset after N losses
    martingale_streak    = db.Column(db.Integer, default=0)    # current consecutive loss count
    cooldown_remaining   = db.Column(db.Integer, default=0)    # candles left to sit out
    cooldown_loss_count  = db.Column(db.Integer, default=0)    # consecutive losses since last win
    cooldown_win_count   = db.Column(db.Integer, default=0)    # consecutive wins since last loss (invert mode)
    use_cooldown         = db.Column(db.Boolean, default=False) # manual toggle: sit out 2 candles after 2 losses
    stop_loss_balance    = db.Column(db.Float, default=None)    # stop trading when balance hits this value
    use_family_rotation  = db.Column(db.Boolean, default=False) # rotate signal families (A→B→C)
    pair_loss_cooldowns  = db.Column(db.Text, default='{}')    # JSON: per-pair cooldown state
    dir_saturation_history = db.Column(db.Text, default='[]')  # JSON: last 6 [{dir,result}] for Rule 2
    use_limitless        = db.Column(db.Boolean, default=True)  # toggle Limitless execution
    use_polymarket       = db.Column(db.Boolean, default=False) # toggle Polymarket execution
    # Order execution method, independent per venue. Both platforms' CLOBs
    # support the same two shapes for this bot's purposes:
    #   GTC — resting limit order at max_contract_price / poly_max_price.
    #         Can sit unfilled if the market never reaches that price.
    #   FOK — fill-or-kill "market" order: executes immediately in full at
    #         the best available price, or is cancelled entirely (no partial
    #         fills, never rests on the book). max_contract_price /
    #         poly_max_price still applies as the worst acceptable price
    #         (slippage protection) — FOK isn't "any price whatsoever", it's
    #         "immediately, or not at all, at up to this price".
    # The on-chain order payload (makerAmount/takerAmount/signature) is
    # identical either way — only the orderType field sent to the API
    # differs. See place_live_order in limitless_executor.py / polymarket_executor.py.
    limitless_order_type = db.Column(db.String(4), default='GTC')
    poly_order_type      = db.Column(db.String(4), default='GTC')
    poly_position_size   = db.Column(db.Float,   default=10.0)  # Polymarket BASE position size USD (used directly if martingale is off)
    poly_max_price       = db.Column(db.Float,   default=0.50)  # Polymarket max contract price
    poly_fill_threshold_pct    = db.Column(db.Float, default=95.0)
    # Fully independent martingale ladder for Polymarket — separate sequence,
    # cap, and streak from Limitless's. Gated on Polymarket's OWN confirmed
    # fill (poly_fill == FILLED, i.e. fill_ratio >= poly_fill_threshold_pct)
    # and OWN outcome (poly_outcome, from Polymarket's own resolution) —
    # mirrors the Limitless martingale block in job_resolve_outcomes exactly,
    # just on Polymarket's data instead of Limitless's. A partial/zero fill
    # freezes this streak the same way it freezes Limitless's.
    use_poly_martingale       = db.Column(db.Boolean, default=False)
    poly_martingale_sequence  = db.Column(db.String(200), default='1,1.5,2,3,4.5,6.7')
    poly_martingale_cap       = db.Column(db.Integer, default=10)
    poly_martingale_streak    = db.Column(db.Integer, default=0)
    max_contract_price   = db.Column(db.Float, default=0.50)
    min_confidence       = db.Column(db.Float, default=0.0)   # 0.0 = disabled; per-pair thresholds in PAIR_CONFIG are the real gates
    no_execute_pairs     = db.Column(db.Text, default='[]')  # JSON list: signal fires but NO live order placed
    cooldown_log         = db.Column(db.Text, default='[]')   # JSON list of cooldown events [{ts,pair,reason,candles,tier}]
    # Outcome now resolves from Limitless's own winningOutcomeIndex (Pyth-fed)
    # instead of the OKX candle whenever the market has resolved in time —
    # this toggle exists purely as a rollback switch back to legacy OKX-only behaviour.
    use_limitless_resolution     = db.Column(db.Boolean, default=True)
    # Minimum % of intended stake that must have actually settled on-chain
    # for a trade to count as a "complete" fill for martingale purposes.
    # Below this (but >0%) the trade is logged as PARTIAL and the streak is
    # frozen, same as a 0% fill, since the pre-defined stake ladder assumes
    # each level's full dollar amount was genuinely at risk.
    martingale_fill_threshold_pct = db.Column(db.Float, default=95.0)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'mode':                   self.mode,
            'position_size':          self.position_size,
            'use_martingale':         self.use_martingale,
            'martingale_sequence':    self.martingale_sequence or '1,1.5,2,3,4.5,6.7',
            'martingale_step':        self.martingale_step,
            'martingale_cap':         self.martingale_cap,
            'martingale_streak':      self.martingale_streak,
            'cooldown_remaining':     self.cooldown_remaining,
            'cooldown_loss_count':    self.cooldown_loss_count,
            'cooldown_win_count':     self.cooldown_win_count,
            'use_cooldown':           bool(self.use_cooldown),
            'stop_loss_balance':      self.stop_loss_balance,
            'use_family_rotation':    bool(self.use_family_rotation),
            'pair_loss_cooldowns':    self.pair_loss_cooldowns or '{}',
            'use_limitless':          bool(self.use_limitless if self.use_limitless is not None else True),
            'use_polymarket':         bool(self.use_polymarket),
            'limitless_order_type':   self.limitless_order_type or 'GTC',
            'poly_order_type':        self.poly_order_type or 'GTC',
            'poly_position_size':     self.poly_position_size or 10.0,
            'poly_max_price':         self.poly_max_price or 0.50,
            'use_poly_martingale':        bool(self.use_poly_martingale),
            'poly_martingale_sequence':   self.poly_martingale_sequence or '1,1.5,2,3,4.5,6.7',
            'poly_martingale_cap':        self.poly_martingale_cap or 10,
            'poly_martingale_streak':     self.poly_martingale_streak or 0,
            'poly_fill_threshold_pct':    self.poly_fill_threshold_pct if self.poly_fill_threshold_pct is not None else 95.0,
            'max_contract_price':     self.max_contract_price,
            'min_confidence':         self.min_confidence,
            'no_execute_pairs':       self.no_execute_pairs or '[]',
            'cooldown_log':           self.cooldown_log or '[]',
            'use_limitless_resolution':      bool(self.use_limitless_resolution if self.use_limitless_resolution is not None else True),
            'martingale_fill_threshold_pct': self.martingale_fill_threshold_pct if self.martingale_fill_threshold_pct is not None else 95.0,
        }


class ShadowBalance(db.Model):
    __tablename__ = 'shadow_balance'

    id                = db.Column(db.Integer, primary_key=True)
    balance           = db.Column(db.Float, default=1000.0)
    total_profit_loss = db.Column(db.Float, default=0.0)
    updated_at        = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'balance':           round(self.balance, 2),
            'total_profit_loss': round(self.total_profit_loss, 2),
        }
