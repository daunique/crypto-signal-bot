/**
 * db.js — Supabase Postgres via Transaction Pooler (port 6543)
 * ═══════════════════════════════════════════════════════════════
 * Uses the pg (node-postgres) package with Supabase's Transaction
 * Pooler connection string which works on IPv4 (Render free plan).
 *
 * Required env var:
 *   DATABASE_URL=postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres
 *
 * Get it from: Supabase → Project → Settings → Database →
 *   Connection string → Transaction pooler → URI
 */

import pg from "pg";

const { Pool } = pg;

let pool = null;

function getPool() {
  if (!pool) {
    if (!process.env.DATABASE_URL) {
      console.warn("[db] DATABASE_URL not set — running without persistence");
      return null;
    }
    pool = new Pool({
      connectionString: process.env.DATABASE_URL,
      ssl: { rejectUnauthorized: false },  // required for Supabase
      max: 3,           // keep connection count low on free plan
      idleTimeoutMillis: 30000,
      connectionTimeoutMillis: 5000,
    });
    pool.on("error", (err) => {
      console.error("[db] Pool error:", err.message);
    });
  }
  return pool;
}

// ── SCHEMA ────────────────────────────────────────────────────────

export async function initDB() {
  const db = getPool();
  if (!db) return false;
  try {
    await db.query(`
      CREATE TABLE IF NOT EXISTS signals (
        id              TEXT PRIMARY KEY,
        pair            TEXT        NOT NULL,
        direction       TEXT        NOT NULL,
        confidence      INTEGER     NOT NULL,
        market_condition TEXT,
        rsi             INTEGER,
        family          INTEGER,
        open_price      NUMERIC(20,8) NOT NULL,
        close_price     NUMERIC(20,8),
        result          TEXT        DEFAULT 'PENDING',
        order_id        TEXT,
        order_mode      TEXT,
        order_shadow    BOOLEAN     DEFAULT true,
        contracts       NUMERIC(12,4),
        price_per_contract NUMERIC(10,4),
        candle_start    BIGINT,
        timestamp       BIGINT      NOT NULL,
        settled_at      BIGINT,
        created_at      TIMESTAMPTZ DEFAULT NOW()
      );

      CREATE INDEX IF NOT EXISTS signals_timestamp_idx ON signals (timestamp DESC);
      CREATE INDEX IF NOT EXISTS signals_pair_idx      ON signals (pair);
      CREATE INDEX IF NOT EXISTS signals_result_idx    ON signals (result);
      CREATE INDEX IF NOT EXISTS signals_candle_idx    ON signals (candle_start);

      CREATE TABLE IF NOT EXISTS daily_stats (
        date   DATE PRIMARY KEY,
        wins   INTEGER DEFAULT 0,
        losses INTEGER DEFAULT 0,
        total  INTEGER DEFAULT 0
      );

      CREATE TABLE IF NOT EXISTS demo_trades (
        id              TEXT PRIMARY KEY,
        signal_id       TEXT,
        pair            TEXT        NOT NULL,
        direction       TEXT        NOT NULL,
        open_price      NUMERIC(20,8) NOT NULL,
        close_price     NUMERIC(20,8),
        result          TEXT        DEFAULT 'PENDING',
        contracts       NUMERIC(12,4),
        stake_usd       NUMERIC(10,2),
        pnl_usd         NUMERIC(10,4),
        candle_start    BIGINT,
        timestamp       BIGINT      NOT NULL,
        settled_at      BIGINT
      );

      CREATE INDEX IF NOT EXISTS demo_timestamp_idx ON demo_trades (timestamp DESC);
    `);
    console.log("[db] Schema ready");
    return true;
  } catch (e) {
    console.error("[db] initDB error:", e.message);
    return false;
  }
}

// ── SIGNALS ───────────────────────────────────────────────────────

export async function saveSignal(signal) {
  const db = getPool();
  if (!db) return;
  try {
    await db.query(`
      INSERT INTO signals
        (id, pair, direction, confidence, market_condition, rsi, family,
         open_price, close_price, result, order_id, order_mode, order_shadow,
         contracts, price_per_contract, candle_start, timestamp, settled_at)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18)
      ON CONFLICT (id) DO UPDATE SET
        close_price        = EXCLUDED.close_price,
        result             = EXCLUDED.result,
        order_id           = EXCLUDED.order_id,
        order_mode         = EXCLUDED.order_mode,
        order_shadow       = EXCLUDED.order_shadow,
        contracts          = EXCLUDED.contracts,
        price_per_contract = EXCLUDED.price_per_contract,
        settled_at         = EXCLUDED.settled_at
    `, [
      signal.id,
      signal.pair,
      signal.direction,
      signal.confidence,
      signal.marketCondition || null,
      signal.rsi || null,
      signal.family ?? null,
      signal.openPrice,
      signal.closePrice ?? null,
      signal.result || "PENDING",
      signal.orderResult?.order_id || null,
      signal.orderResult?.shadow ? "shadow" : "live",
      signal.orderResult?.shadow ?? true,
      signal.orderResult?.contracts ?? null,
      signal.orderResult?.price_per_contract ?? null,
      signal.candleStart ?? null,
      signal.timestamp,
      signal.settledAt ?? null,
    ]);
  } catch (e) {
    console.error("[db] saveSignal error:", e.message);
  }
}

export async function getSignalsByDate(dateStr) {
  // dateStr = "YYYY-MM-DD" in UTC
  const db = getPool();
  if (!db) return [];
  try {
    const res = await db.query(`
      SELECT * FROM signals
      WHERE DATE(TO_TIMESTAMP(timestamp / 1000) AT TIME ZONE 'UTC') = $1
      ORDER BY timestamp DESC
    `, [dateStr]);
    return res.rows.map(rowToSignal);
  } catch (e) {
    console.error("[db] getSignalsByDate error:", e.message);
    return [];
  }
}

export async function getAvailableDates() {
  const db = getPool();
  if (!db) return [];
  try {
    const res = await db.query(`
      SELECT DISTINCT DATE(TO_TIMESTAMP(timestamp / 1000) AT TIME ZONE 'UTC') AS date
      FROM signals
      ORDER BY date DESC
      LIMIT 90
    `);
    return res.rows.map(r => r.date.toISOString().slice(0, 10));
  } catch (e) {
    console.error("[db] getAvailableDates error:", e.message);
    return [];
  }
}

export async function getRecentSignals(limit = 200) {
  const db = getPool();
  if (!db) return [];
  try {
    const res = await db.query(
      `SELECT * FROM signals ORDER BY timestamp DESC LIMIT $1`, [limit]
    );
    return res.rows.map(rowToSignal);
  } catch (e) {
    console.error("[db] getRecentSignals error:", e.message);
    return [];
  }
}

function rowToSignal(r) {
  return {
    id:              r.id,
    pair:            r.pair,
    direction:       r.direction,
    confidence:      r.confidence,
    marketCondition: r.market_condition,
    rsi:             r.rsi,
    family:          r.family,
    openPrice:       parseFloat(r.open_price),
    closePrice:      r.close_price != null ? parseFloat(r.close_price) : null,
    result:          r.result,
    candleStart:     r.candle_start ? Number(r.candle_start) : null,
    timestamp:       Number(r.timestamp),
    settledAt:       r.settled_at ? Number(r.settled_at) : null,
    orderResult: r.order_id ? {
      success:           true,
      order_id:          r.order_id,
      shadow:            r.order_shadow,
      contracts:         r.contracts ? parseFloat(r.contracts) : null,
      price_per_contract: r.price_per_contract ? parseFloat(r.price_per_contract) : null,
    } : null,
  };
}

// ── DAILY STATS ───────────────────────────────────────────────────

export async function upsertDailyStats(dateStr, stats) {
  const db = getPool();
  if (!db) return;
  try {
    await db.query(`
      INSERT INTO daily_stats (date, wins, losses, total)
      VALUES ($1, $2, $3, $4)
      ON CONFLICT (date) DO UPDATE SET
        wins   = EXCLUDED.wins,
        losses = EXCLUDED.losses,
        total  = EXCLUDED.total
    `, [dateStr, stats.wins, stats.losses, stats.total]);
  } catch (e) {
    console.error("[db] upsertDailyStats error:", e.message);
  }
}

export async function getDailyStats(dateStr) {
  const db = getPool();
  if (!db) return null;
  try {
    const res = await db.query(`SELECT * FROM daily_stats WHERE date = $1`, [dateStr]);
    return res.rows[0] || null;
  } catch (e) {
    console.error("[db] getDailyStats error:", e.message);
    return null;
  }
}

export async function getAllDailyStats() {
  const db = getPool();
  if (!db) return [];
  try {
    const res = await db.query(`SELECT * FROM daily_stats ORDER BY date DESC LIMIT 90`);
    return res.rows;
  } catch (e) { return []; }
}

// ── DEMO TRADES ───────────────────────────────────────────────────

export async function saveDemoTrade(trade) {
  const db = getPool();
  if (!db) return;
  try {
    await db.query(`
      INSERT INTO demo_trades
        (id, signal_id, pair, direction, open_price, close_price,
         result, contracts, stake_usd, pnl_usd, candle_start, timestamp, settled_at)
      VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
      ON CONFLICT (id) DO UPDATE SET
        close_price = EXCLUDED.close_price,
        result      = EXCLUDED.result,
        pnl_usd     = EXCLUDED.pnl_usd,
        settled_at  = EXCLUDED.settled_at
    `, [
      trade.id, trade.signalId, trade.pair, trade.direction,
      trade.openPrice, trade.closePrice ?? null,
      trade.result || "PENDING",
      trade.contracts ?? null,
      trade.stakeUsd ?? null,
      trade.pnlUsd ?? null,
      trade.candleStart ?? null,
      trade.timestamp,
      trade.settledAt ?? null,
    ]);
  } catch (e) {
    console.error("[db] saveDemoTrade error:", e.message);
  }
}

export async function getDemoTrades(limit = 100) {
  const db = getPool();
  if (!db) return [];
  try {
    const res = await db.query(
      `SELECT * FROM demo_trades ORDER BY timestamp DESC LIMIT $1`, [limit]
    );
    return res.rows.map(r => ({
      id:         r.id,
      signalId:   r.signal_id,
      pair:       r.pair,
      direction:  r.direction,
      openPrice:  parseFloat(r.open_price),
      closePrice: r.close_price != null ? parseFloat(r.close_price) : null,
      result:     r.result,
      contracts:  r.contracts ? parseFloat(r.contracts) : null,
      stakeUsd:   r.stake_usd ? parseFloat(r.stake_usd) : null,
      pnlUsd:     r.pnl_usd   ? parseFloat(r.pnl_usd)  : null,
      candleStart: r.candle_start ? Number(r.candle_start) : null,
      timestamp:  Number(r.timestamp),
      settledAt:  r.settled_at ? Number(r.settled_at) : null,
    }));
  } catch (e) {
    console.error("[db] getDemoTrades error:", e.message);
    return [];
  }
}

export async function getDemoStats() {
  const db = getPool();
  if (!db) return { wins:0, losses:0, total:0, pnl:0 };
  try {
    const res = await db.query(`
      SELECT
        COUNT(*) FILTER (WHERE result='WIN')  AS wins,
        COUNT(*) FILTER (WHERE result='LOSS') AS losses,
        COUNT(*) FILTER (WHERE result IN ('WIN','LOSS')) AS total,
        COALESCE(SUM(pnl_usd),0) AS pnl
      FROM demo_trades
      WHERE DATE(TO_TIMESTAMP(timestamp/1000) AT TIME ZONE 'UTC') = CURRENT_DATE
    `);
    const r = res.rows[0];
    return { wins: Number(r.wins), losses: Number(r.losses), total: Number(r.total), pnl: parseFloat(r.pnl) };
  } catch (e) { return { wins:0, losses:0, total:0, pnl:0 }; }
}
