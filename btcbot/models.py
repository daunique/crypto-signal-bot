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
    position_size     = db.Column(db.Float)
    contracts_bought  = db.Column(db.Float)
    contract_price    = db.Column(db.Float)
    telegram_sent     = db.Column(db.Boolean, default=False)

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
            'position_size':     self.position_size,
            'contracts_bought':  self.contracts_bought,
            'contract_price':    self.contract_price,
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
    martingale_multiplier= db.Column(db.Float, default=2.0)
    max_contract_price   = db.Column(db.Float, default=0.50)
    min_confidence       = db.Column(db.Float, default=0.58)
    updated_at           = db.Column(db.DateTime, default=datetime.utcnow)

    def to_dict(self):
        return {
            'mode':                   self.mode,
            'position_size':          self.position_size,
            'use_martingale':         self.use_martingale,
            'martingale_multiplier':  self.martingale_multiplier,
            'max_contract_price':     self.max_contract_price,
            'min_confidence':         self.min_confidence,
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
