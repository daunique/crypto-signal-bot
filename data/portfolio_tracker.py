"""
POLYBOT — Portfolio Tracker
Aggregates wallet, PnL, and trade stats for the dashboard.
"""


class PortfolioTracker:
    def __init__(self, journal):
        self.journal = journal
        self.current_balance = 0.0
        self.deployed = 0.0
        self.system_status = "STARTING"

    def update_balance(self, balance: float):
        self.current_balance = balance

    def update_deployed(self, deployed: float):
        self.deployed = deployed

    def set_status(self, status: str):
        self.system_status = status

    def get_summary(self) -> dict:
        return {
            "balance":      round(self.current_balance, 4),
            "daily_pnl":    round(self.journal.get_daily_pnl(), 4),
            "total_pnl":    round(self.journal.get_total_pnl(), 4),
            "trades_today": self.journal.get_trades_today(),
            "win_rate":     round(self.journal.get_win_rate(), 2),
            "deployed":     round(self.deployed, 4),
            "available":    round(self.current_balance - self.deployed, 4),
            "avg_edge":     self.journal.get_avg_edge(),
            "status":       self.system_status,
        }

    def get_recent_trades(self, limit: int = 20) -> list:
        return self.journal.get_recent_trades(limit)
