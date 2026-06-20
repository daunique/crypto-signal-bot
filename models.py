from datetime import datetime
from extensions import db


class Signal(db.Model):
    __tablename__ = 'signals'

    id                = db.Column(db.Integer, primary_key=True)
    created_at        = db.Column(db.DateTime, default=datetime.utcnow)
    symbol            = db.Column(db.String(20), nullable=False)
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
    limitless_fill    = db.Column(db.String(10), default='NEUTRAL')
    tx_hash           = db.Column(db.String(80),  default=None) # on-chain tx hash once Limitless confirms the fill
    fill_check_status = db.Column(db.String(20),  default=None) # PENDING_CHECK / FILLED / UNFILLED / NEUTRAL
    fill_check_count  = db.Column(db.Integer,      default=0)   # how many times we've polled for a fill
    best_entry_pct    = db.Column(db.Float, default=None)
    poly_order_id     = db.Column(db.String(120), default=None) # Polymarket order ID
    poly_fill         = db.Column(db.String(10),  default='NEUTRAL') # FILLED/UNFILLED/NEUTRAL  # FILLED, UNFILLED, NEUTRAL
    maker_address     = db.Column(db.String(64),  default=None) # on-chain smart wallet (maker) used for this live order
    signer_address    = db.Column(db.String(64),  default=None) # on-chain EOA signer used for this live order

    def to_dict(self):
        return {
            'id':                self.id,
            'created_at':        self.created_at.isoformat() if self.created_at else None,
            'symbol':            self.symbol,
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
            'maker_address':     self.maker_address,
            'signer_address':    self.signer_address,
            'tx_hash':           self.tx_hash,
            'fill_check_status': self.fill_check_status,
            'fill_check_count':  self.fill_check_count or 0,
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
    poly_position_size   = db.Column(db.Float,   default=10.0)  # Polymarket position size USD
    poly_max_price       = db.Column(db.Float,   default=0.50)  # Polymarket max contract price
    max_contract_price   = db.Column(db.Float, default=0.50)
    min_confidence       = db.Column(db.Float, default=0.0)   # 0.0 = disabled; per-pair thresholds in PAIR_CONFIG are the real gates
    no_execute_pairs     = db.Column(db.Text, default='["XRP-USDT"]')  # JSON list: signal fires but NO live order placed
    cooldown_log         = db.Column(db.Text, default='[]')   # JSON list of cooldown events [{ts,pair,reason,candles,tier}]
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
            'poly_position_size':     self.poly_position_size or 10.0,
            'poly_max_price':         self.poly_max_price or 0.50,
            'max_contract_price':     self.max_contract_price,
            'min_confidence':         self.min_confidence,
            'no_execute_pairs':       self.no_execute_pairs or '["XRP-USDT"]',
            'cooldown_log':           self.cooldown_log or '[]',
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
