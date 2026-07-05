"""
POLYBOT — Trade Journal
SQLite-backed logging of every trade, wallet snapshot,
and circuit breaker event.
"""
import sqlite3
import os
from datetime import datetime


DB_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "trades.db"
)


class TradeJournal:
    def __init__(self, db_path: str = DB_PATH):
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        # WAL mode allows one writer (the trading loop) and multiple
        # concurrent readers (the Flask dashboard thread) without
        # "database is locked" errors — this bot writes continuously
        # from asyncio while the dashboard reads from a separate
        # thread, which is exactly the pattern WAL is designed for.
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self._create_tables()

    def _create_tables(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            pair_id         TEXT NOT NULL,
            slug            TEXT,
            yes_token       TEXT,
            no_token        TEXT,
            yes_ask         REAL,
            no_ask          REAL,
            combined_cost   REAL,
            gross_edge      REAL,
            size            REAL,
            yes_fill_price  REAL,
            no_fill_price   REAL,
            actual_cost     REAL,
            taker_fee_yes   REAL,
            taker_fee_no    REAL,
            slippage        REAL,
            gas_fee         REAL,
            total_fee       REAL,
            net_profit      REAL,
            status          TEXT,
            hit_number      INTEGER,
            time_remaining  REAL,
            capital_mode    TEXT,
            expiry          REAL
        );

        CREATE TABLE IF NOT EXISTS wallet_snapshots (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            balance     REAL NOT NULL,
            deployed    REAL,
            pnl_today   REAL,
            pnl_total   REAL
        );

        CREATE TABLE IF NOT EXISTS circuit_breaker_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp   TEXT NOT NULL,
            trigger     TEXT NOT NULL,
            action      TEXT
        );

        CREATE TABLE IF NOT EXISTS observed_opportunities (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            pair_id         TEXT NOT NULL,
            slug            TEXT,
            combined_cost   REAL,
            gross_edge      REAL,
            shares          REAL,
            net_profit      REAL,
            time_remaining  REAL
        );

        CREATE INDEX IF NOT EXISTS idx_trades_timestamp
            ON trades(timestamp);
        CREATE INDEX IF NOT EXISTS idx_trades_pair
            ON trades(pair_id);
        CREATE INDEX IF NOT EXISTS idx_observed_timestamp
            ON observed_opportunities(timestamp);
        """)
        self.conn.commit()

    def log_trade(self, t: dict):
        self.conn.execute("""
            INSERT INTO trades (
                timestamp, pair_id, slug, yes_token, no_token,
                yes_ask, no_ask, combined_cost, gross_edge, size,
                yes_fill_price, no_fill_price, actual_cost,
                taker_fee_yes, taker_fee_no, slippage, gas_fee,
                total_fee, net_profit, status, hit_number,
                time_remaining, capital_mode, expiry
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            t.get("pair_id"), t.get("slug"),
            t.get("yes_token"), t.get("no_token"),
            t.get("yes_ask"), t.get("no_ask"),
            t.get("combined_cost"), t.get("gross_edge"), t.get("size"),
            t.get("yes_fill_price"), t.get("no_fill_price"),
            t.get("actual_cost"), t.get("taker_fee_yes"),
            t.get("taker_fee_no"), t.get("slippage"), t.get("gas_fee"),
            t.get("total_fee"), t.get("net_profit"), t.get("status"),
            t.get("hit_number"), t.get("time_remaining"),
            t.get("capital_mode"), t.get("expiry"),
        ))
        self.conn.commit()

    def log_wallet(self, balance: float, deployed: float,
                    pnl_today: float, pnl_total: float):
        self.conn.execute("""
            INSERT INTO wallet_snapshots
            (timestamp, balance, deployed, pnl_today, pnl_total)
            VALUES (?,?,?,?,?)
        """, (datetime.utcnow().isoformat(),
              balance, deployed, pnl_today, pnl_total))
        self.conn.commit()

    def log_circuit_break(self, reason: str):
        self.conn.execute("""
            INSERT INTO circuit_breaker_log (timestamp, trigger, action)
            VALUES (?, ?, 'HALT')
        """, (datetime.utcnow().isoformat(), reason))
        self.conn.commit()

    def log_observed_opportunity(self, o: dict):
        """
        Logs an edge that WOULD have been traded if this pair's
        duration weren't currently toggled off — used to build a
        real 5min-vs-15min comparison even while only one is live.
        """
        self.conn.execute("""
            INSERT INTO observed_opportunities (
                timestamp, pair_id, slug, combined_cost,
                gross_edge, shares, net_profit, time_remaining
            ) VALUES (?,?,?,?,?,?,?,?)
        """, (
            datetime.utcnow().isoformat(),
            o.get("pair_id"), o.get("slug"), o.get("combined_cost"),
            o.get("gross_edge"), o.get("shares"), o.get("net_profit"),
            o.get("time_remaining"),
        ))
        self.conn.commit()

    def get_daily_pnl(self) -> float:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cur = self.conn.execute("""
            SELECT COALESCE(SUM(net_profit), 0) FROM trades
            WHERE timestamp LIKE ? AND status = 'HEDGED'
        """, (f"{today}%",))
        return cur.fetchone()[0]

    def get_total_pnl(self) -> float:
        cur = self.conn.execute("""
            SELECT COALESCE(SUM(net_profit), 0) FROM trades
            WHERE status = 'HEDGED'
        """)
        return cur.fetchone()[0]

    def get_trades_today(self) -> int:
        today = datetime.utcnow().strftime("%Y-%m-%d")
        cur = self.conn.execute("""
            SELECT COUNT(*) FROM trades WHERE timestamp LIKE ?
        """, (f"{today}%",))
        return cur.fetchone()[0]

    def get_win_rate(self) -> float:
        cur = self.conn.execute("""
            SELECT COUNT(CASE WHEN net_profit > 0 THEN 1 END) * 100.0
                   / NULLIF(COUNT(*), 0)
            FROM trades WHERE status = 'HEDGED'
        """)
        return cur.fetchone()[0] or 0.0

    def get_avg_edge(self) -> float:
        cur = self.conn.execute("""
            SELECT AVG(gross_edge) FROM trades WHERE status = 'HEDGED'
        """)
        return round(cur.fetchone()[0] or 0.0, 4)

    def get_duration_comparison(self) -> dict:
        """
        Combines REAL executed trades with OBSERVED (would-have-
        traded) opportunities to build a fair 5min-vs-15min
        comparison, regardless of which duration is currently
        toggled live. This is what actually answers "which one
        should I scale into" — a duration that's been toggled off
        the whole time still shows real profit potential here.
        """
        result = {}
        for label, suffix in (("5MIN", "_5MIN"), ("15MIN", "_15MIN")):
            like_pattern = f"%{suffix}"

            # Real executed trades for this duration
            cur = self.conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(net_profit), 0),
                       COALESCE(AVG(gross_edge), 0),
                       COUNT(CASE WHEN net_profit > 0 THEN 1 END)
                FROM trades
                WHERE pair_id LIKE ? AND status = 'HEDGED'
            """, (like_pattern,))
            real_count, real_profit, real_avg_edge, real_wins = cur.fetchone()

            # Observed-only opportunities (toggled off at the time)
            cur = self.conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(net_profit), 0),
                       COALESCE(AVG(gross_edge), 0)
                FROM observed_opportunities
                WHERE pair_id LIKE ?
            """, (like_pattern,))
            obs_count, obs_profit, obs_avg_edge = cur.fetchone()

            total_count = real_count + obs_count
            total_profit = real_profit + obs_profit
            win_rate = (real_wins * 100.0 / real_count) if real_count else 0.0

            result[label] = {
                "real_trades":        real_count,
                "real_profit":        round(real_profit, 4),
                "real_win_rate":      round(win_rate, 2),
                "observed_only":      obs_count,
                "observed_profit":    round(obs_profit, 4),
                "combined_opportunities": total_count,
                "combined_potential_profit": round(total_profit, 4),
                "avg_edge": round(
                    (real_avg_edge if real_count else obs_avg_edge)
                    if not (real_count and obs_count) else
                    ((real_avg_edge * real_count + obs_avg_edge * obs_count)
                     / total_count),
                    4
                ) if total_count else 0.0,
            }
        return result

    def get_recent_trades(self, limit: int = 20) -> list:
        cur = self.conn.execute("""
            SELECT timestamp, pair_id, size, gross_edge,
                   net_profit, status
            FROM trades ORDER BY timestamp DESC LIMIT ?
        """, (limit,))
        cols = ["time", "pair", "size", "edge", "profit", "status"]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
