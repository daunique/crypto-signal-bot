#!/usr/bin/env python3
"""
Polymarket Momentum-Crossover Bot — crypto 5m/15m Up/Down markets
===================================================================

WHAT THIS IS
------------
Automates the momentum-crossover signal backtested in our analysis:
  - At K1 seconds after a market opens, record YES/NO mid-price.
  - At K2 seconds after open, record it again.
  - Buy whichever side gained more ground, at that moment's ask price.
  - Hold to resolution. No exit logic — this is buy-and-hold-to-close.

READ THIS BEFORE YOU FLIP DRY_RUN OFF
--------------------------------------
1. STATISTICAL BASIS IS THIN. This was validated on 33-53 trades across two
   capture sessions. That is not enough to be confident it's a real edge
   rather than noise (see the sample-size math we worked out: ~190 trades
   needed just to distinguish a 65% win rate from a 55% breakeven at normal
   confidence). Treat this bot as continued paper-testing, not a "system."

2. $0.20/TRADE WILL LIKELY BE REJECTED. Polymarket's CLOB enforces a
   minimum order size (5 shares as of this writing). At the entry prices
   this strategy actually trades at (30-85c/share in backtests), 5 shares
   costs $1.50-$4.25 — well above $0.20. This script computes shares =
   stake/price and SKIPS (does not force) any trade that falls below the
   exchange minimum, logging it instead. Practically: most $0.20 attempts
   will be skipped, not executed. Raise STAKE_USD if you want trades to
   actually go through — see README for the math.

3. NO SANDBOX EXISTS. Polymarket has no testnet/paper-trading mode for
   real order placement. DRY_RUN in this script is a local simulation
   (real prices, no real order sent) — it is the only "practice" you get
   before real money is at risk. Run DRY_RUN for a while first.

4. GEOGRAPHIC RESTRICTIONS ARE REAL AND CHECKED AT STARTUP. As of this
   writing the main Polymarket CLOB blocks order placement from the US,
   UK, France, Germany, Italy, Netherlands, Belgium, Australia, and others
   (Polymarket US, a separate CFTC-regulated product with a completely
   different API, exists for US persons — this script does NOT talk to
   that product). This script calls the official geoblock endpoint before
   doing anything live and refuses to trade if you're blocked. It cannot
   tell you whether you legally *should* be using this product — that's
   on you to confirm.

5. THE SDK ECOSYSTEM HERE IS YOUNG AND MOVES FAST. Polymarket did a hard,
   breaking migration to "CLOB V2" recently; old libraries and old signed
   orders stopped working entirely. This script is written against
   py-clob-client-v2 as documented at the time this was written. Check
   https://github.com/Polymarket/py-clob-client-v2 before relying on this
   long-term — pin your installed version and re-read the README if
   anything here throws unexpected errors.

6. THIS IS NOT FINANCIAL ADVICE and I'm not a financial advisor. You are
   choosing to risk real money on a thin backtest. That's your call to
   make, not mine — this script just tries to do it as safely and
   transparently as possible.

SETUP
-----
See README.md in this same folder for full setup steps (Fly.io and/or Termux).
Required environment variables (Fly: `fly secrets set`; local: a .env file —
see .env.example):
  POLY_PRIVATE_KEY      - your wallet's private key. NEVER commit this.
  POLY_FUNDER_ADDRESS   - your Polymarket proxy wallet address (Settings page).
                          Required whenever POLY_SIGNATURE_TYPE != 0.
  POLY_SIGNATURE_TYPE   - 0 = plain EOA (you hold the private key directly and
                          it IS the funding address)
                          1 = email/Magic-link wallet (default here) — the
                          private key signs, but funds live at a separate
                          proxy address, which is POLY_FUNDER_ADDRESS
                          2 = browser-extension wallet proxy
                          (Deposit wallets / type 3 need the separate relayer
                          flow — see README, not implemented here.)

If you signed up to Polymarket with email/Google rather than connecting a
wallet like MetaMask, you're on signature_type 1. Find both values on
Polymarket's Settings page: the proxy wallet address is shown directly: the
private key for the underlying signing key is under the export/reveal
private key option. Treat it exactly like any other private key — anyone
who has it can move your funds.

Run modes:
  python3 momentum_bot.py --check       # environment + auth + geoblock check only
  python3 momentum_bot.py --dry-run     # simulate trades with real live prices, no real orders
  python3 momentum_bot.py --live        # place real orders (asks for typed confirmation)

On Fly.io (no terminal attached), --live instead requires the secret
CONFIRM_LIVE_TRADING to be set to the exact phrase "I UNDERSTAND THE RISK" —
see README.md.
"""

import os
import sys
import csv
import json
import time
import math
import asyncio
import logging
import argparse
from pathlib import Path
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, field

try:
    import requests
except ImportError:
    sys.exit("Missing dependency 'requests'. Run: pip install requests")

try:
    import websockets
except ImportError:
    sys.exit("Missing dependency 'websockets'. Run: pip install websockets")

try:
    from py_clob_client_v2 import (
        ClobClient, ApiCreds, OrderArgs, OrderType, Side,
        PartialCreateOrderOptions,
    )
except ImportError:
    sys.exit(
        "Missing dependency 'py-clob-client-v2'. Run: pip install py-clob-client-v2\n"
        "(NOT 'py-clob-client' — that's the retired V1 package and no longer works.)"
    )

# python-dotenv is optional; fall back to plain os.environ if not installed
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


# ============================================================================
# CONFIG — override any of this via environment variables (see .env.example)
# ============================================================================

@dataclass
class Config:
    # --- Auth ---
    private_key: str = field(default_factory=lambda: os.environ.get("POLY_PRIVATE_KEY", ""))
    funder: str = field(default_factory=lambda: os.environ.get("POLY_FUNDER_ADDRESS", ""))
    signature_type: int = field(default_factory=lambda: int(os.environ.get("POLY_SIGNATURE_TYPE", "1")))
    chain_id: int = 137
    host: str = "https://clob.polymarket.com"
    ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

    # --- Strategy parameters (defaults = what we actually validated) ---
    k1_seconds: int = int(os.environ.get("K1_SECONDS", "60"))
    k2_seconds: int = int(os.environ.get("K2_SECONDS", "90"))
    tolerance_seconds: float = float(os.environ.get("TOL_SECONDS", "5"))
    assets: tuple = tuple(os.environ.get("ASSETS", "BTC,ETH,SOL,XRP,BNB,DOGE,HYPE").split(","))
    durations_min: tuple = tuple(int(x) for x in os.environ.get("DURATIONS_MIN", "5").split(","))

    # --- Money / risk controls ---
    stake_usd: float = float(os.environ.get("STAKE_USD", "0.20"))
    fallback_min_shares: float = float(os.environ.get("FALLBACK_MIN_SHARES", "5"))
    max_trades_per_day: int = int(os.environ.get("MAX_TRADES_PER_DAY", "20"))
    max_daily_loss_usd: float = float(os.environ.get("MAX_DAILY_LOSS_USD", "5.00"))

    # Polymarket's own docs list INVALID_ORDER_MIN_SIZE as a general order-
    # placement error (not documented as GTC-only), and third-party references
    # describe it the same way. But documented behavior and actual matching-
    # engine behavior aren't always identical, and it's plausible a minimum
    # meant to stop dust *resting* orders doesn't apply to a FOK/FAK that fills
    # instantly and never rests. A rejected order costs nothing but an API call
    # (no funds move on rejection), so this is safely testable against the real
    # exchange rather than something to keep arguing about from docs.
    #   True  (default) = skip trades below the minimum locally, never submit them
    #   False            = submit anyway and log whatever the exchange actually
    #                       says — the definitive answer for FOK/FAK specifically
    skip_below_min_size: bool = os.environ.get("SKIP_BELOW_MIN_SIZE", "true").lower() != "false"

    # --- Files ---
    state_dir: Path = Path(os.environ.get("BOT_STATE_DIR", str(Path.home() / ".polymarket_bot")))

    def token_ok(self) -> bool:
        return bool(self.private_key) and self.private_key not in ("", "<your-private-key>")

    def funder_ok(self) -> bool:
        # signature_type 0 (plain EOA) doesn't need a separate funder address —
        # the wallet that signs is the wallet that holds funds. Types 1/2/3 all
        # use a proxy/deposit wallet, so funder is required.
        if self.signature_type == 0:
            return True
        return bool(self.funder) and self.funder not in ("", "<your-proxy-wallet-address>")


CFG = Config()
CFG.state_dir.mkdir(parents=True, exist_ok=True)
TRADE_LOG_PATH = CFG.state_dir / "trade_log.csv"
DAY_STATE_PATH = CFG.state_dir / "day_state.json"
CREDS_CACHE_PATH = CFG.state_dir / "api_creds.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("momentum_bot")


# ============================================================================
# SAFETY: geoblock check (Polymarket's own recommended pre-flight check)
# ============================================================================

def check_geoblock() -> dict:
    """Calls Polymarket's official geoblock endpoint. Returns the raw response."""
    resp = requests.get("https://polymarket.com/api/geoblock", timeout=10)
    resp.raise_for_status()
    return resp.json()


# ============================================================================
# DAY STATE (trades-today / pnl-today, persisted across restarts)
# ============================================================================

def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def load_day_state() -> dict:
    if DAY_STATE_PATH.exists():
        try:
            data = json.loads(DAY_STATE_PATH.read_text())
            if data.get("date") == _today_key():
                return data
        except Exception:
            pass
    return {"date": _today_key(), "trades": 0, "committed_usd": 0.0, "realized_pnl": 0.0}


def save_day_state(state: dict) -> None:
    DAY_STATE_PATH.write_text(json.dumps(state))


# ============================================================================
# TRADE LOG (doubles as your own fresh capture data — same idea as the
# lifecycle_capture files we've been analyzing all along)
# ============================================================================

def log_trade(row: dict) -> None:
    is_new = not TRADE_LOG_PATH.exists()
    with open(TRADE_LOG_PATH, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "timestamp_iso", "slug", "asset", "duration_min", "side", "signal_strength",
            "entry_price", "shares", "stake_usd", "mode", "status", "order_id", "note",
        ])
        if is_new:
            w.writeheader()
        w.writerow(row)


# ============================================================================
# MARKET DISCOVERY (Gamma API, fetch-by-slug — matches the exact slug
# pattern we've seen throughout our own capture data: "{asset}-updown-{dur}-{epoch}")
# ============================================================================

GAMMA_URL = "https://gamma-api.polymarket.com/markets"


def build_slug(asset: str, duration_min: int, epoch_ts: int) -> str:
    dur_label = f"{duration_min}m"
    return f"{asset.lower()}-updown-{dur_label}-{epoch_ts}"


def next_aligned_epoch(duration_min: int, from_ts: float = None) -> int:
    """Next window-boundary timestamp aligned to duration_min*60 seconds."""
    now = from_ts if from_ts is not None else time.time()
    step = duration_min * 60
    return int(math.ceil(now / step) * step)


def fetch_market_by_slug(slug: str) -> dict | None:
    try:
        resp = requests.get(GAMMA_URL, params={"slug": slug}, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list) and data:
            return data[0]
        if isinstance(data, dict) and data.get("slug"):
            return data
    except Exception as e:
        log.warning(f"Gamma lookup failed for {slug}: {e}")
    return None


def parse_market(market: dict) -> dict | None:
    """Extract condition_id, YES token_id, NO token_id, tick_size, neg_risk from a Gamma market object."""
    try:
        outcomes = market.get("outcomes")
        token_ids = market.get("clobTokenIds")
        if isinstance(outcomes, str):
            outcomes = json.loads(outcomes)
        if isinstance(token_ids, str):
            token_ids = json.loads(token_ids)
        if not outcomes or not token_ids or len(outcomes) != 2 or len(token_ids) != 2:
            return None
        yes_idx = 0 if str(outcomes[0]).lower() in ("yes", "up") else 1
        no_idx = 1 - yes_idx
        return {
            "condition_id": market.get("conditionId"),
            "yes_token": token_ids[yes_idx],
            "no_token": token_ids[no_idx],
            "tick_size": str(market.get("orderPriceMinTickSize") or "0.01"),
            "neg_risk": bool(market.get("negRisk", False)),
            "slug": market.get("slug"),
        }
    except Exception as e:
        log.warning(f"Failed to parse market object: {e}")
        return None


# ============================================================================
# PER-MARKET STATE + MOMENTUM SIGNAL
# ============================================================================

@dataclass
class MarketState:
    slug: str
    asset: str
    duration_min: int
    start_ts: float
    expiry_ts: float
    condition_id: str
    yes_token: str
    no_token: str
    tick_size: str = "0.01"
    neg_risk: bool = False
    yes_bid: float = None
    yes_ask: float = None
    no_bid: float = None
    no_ask: float = None
    k1_snapshot: tuple = None   # (yes_mid, no_mid) captured near start+K1
    k2_snapshot: tuple = None   # (yes_mid, no_mid, yes_ask, no_ask) captured near start+K2
    traded: bool = False

    def mids(self):
        if None in (self.yes_bid, self.yes_ask, self.no_bid, self.no_ask):
            return None, None
        return (self.yes_bid + self.yes_ask) / 2.0, (self.no_bid + self.no_ask) / 2.0


class Bot:
    def __init__(self, cfg: Config, mode: str):
        self.cfg = cfg
        self.mode = mode  # "check" | "dry-run" | "live"
        self.markets: dict[str, MarketState] = {}      # slug -> MarketState
        self.token_to_slug: dict[str, str] = {}          # token_id -> slug
        self.subscribed_tokens: set[str] = set()
        self.day_state = load_day_state()
        self.client: ClobClient | None = None

    # ---------------- auth ----------------

    def init_client(self):
        if not self.cfg.token_ok():
            sys.exit("POLY_PRIVATE_KEY is not set. Copy .env.example to .env (or set the Fly secret) and fill it in.")
        if not self.cfg.funder_ok():
            sys.exit(
                f"POLY_SIGNATURE_TYPE={self.cfg.signature_type} requires POLY_FUNDER_ADDRESS to be set — "
                f"this is your Polymarket proxy wallet address (found in Polymarket account Settings, "
                f"not the private key's own address). Set it and try again."
            )

        creds = None
        if CREDS_CACHE_PATH.exists():
            try:
                cached = json.loads(CREDS_CACHE_PATH.read_text())
                creds = ApiCreds(
                    api_key=cached["api_key"],
                    api_secret=cached["api_secret"],
                    api_passphrase=cached["api_passphrase"],
                )
                log.info("Loaded cached API credentials.")
            except Exception:
                creds = None

        kwargs = dict(host=self.cfg.host, chain_id=self.cfg.chain_id, key=self.cfg.private_key)
        if self.cfg.signature_type != 0:
            kwargs["signature_type"] = self.cfg.signature_type
            kwargs["funder"] = self.cfg.funder

        if creds is None:
            log.info("Deriving API credentials from wallet signature (one-time L1 step)...")
            tmp_client = ClobClient(**kwargs)
            derived = tmp_client.create_or_derive_api_key()
            creds = derived
            try:
                CREDS_CACHE_PATH.write_text(json.dumps({
                    "api_key": derived.api_key,
                    "api_secret": derived.api_secret,
                    "api_passphrase": derived.api_passphrase,
                }))
                log.info(f"Cached API credentials to {CREDS_CACHE_PATH}")
            except Exception as e:
                log.warning(f"Could not cache credentials: {e}")

        self.client = ClobClient(creds=creds, **kwargs)
        log.info("CLOB client initialized (L1 + L2 auth ready).")

    def run_checks(self) -> bool:
        ok = True
        geo = check_geoblock()
        if geo.get("blocked"):
            log.error(f"BLOCKED: Polymarket reports your IP ({geo.get('country')}/{geo.get('region')}) "
                      f"is geo-restricted from placing orders. This script will refuse to go live.")
            ok = False
        else:
            log.info(f"Geoblock check passed (country={geo.get('country')}).")

        try:
            self.init_client()
            server_time = self.client.get_server_time()
            log.info(f"CLOB reachable. Server time: {server_time}")
        except Exception as e:
            log.error(f"Could not reach/authenticate to CLOB: {e}")
            ok = False

        if self.cfg.stake_usd * 1.0 < 0.05:
            log.warning("STAKE_USD is extremely small; nearly every trade will be skipped for being below minimum size.")

        return ok

    # ---------------- discovery ----------------

    def discover_and_track(self):
        """Find the currently-open and next-upcoming windows for each asset/duration and start tracking them."""
        now = time.time()
        for asset in self.cfg.assets:
            for dur in self.cfg.durations_min:
                step = dur * 60
                # current window (the one already running) + next window (about to open)
                current_epoch = int(math.floor(now / step) * step)
                for epoch in (current_epoch, current_epoch + step):
                    start_ts = float(epoch)
                    expiry_ts = start_ts + step
                    if expiry_ts < now:
                        continue
                    slug = build_slug(asset, dur, epoch)
                    if slug in self.markets:
                        continue
                    market = fetch_market_by_slug(slug)
                    if not market:
                        continue
                    parsed = parse_market(market)
                    if not parsed:
                        continue
                    ms = MarketState(
                        slug=slug, asset=asset, duration_min=dur,
                        start_ts=start_ts, expiry_ts=expiry_ts,
                        condition_id=parsed["condition_id"],
                        yes_token=parsed["yes_token"], no_token=parsed["no_token"],
                        tick_size=parsed["tick_size"], neg_risk=parsed["neg_risk"],
                    )
                    self.markets[slug] = ms
                    self.token_to_slug[ms.yes_token] = slug
                    self.token_to_slug[ms.no_token] = slug
                    log.info(f"Tracking new market: {slug} (start in {start_ts-now:+.0f}s)")

    def prune_expired(self):
        cutoff = time.time() - 30
        expired = [s for s, m in self.markets.items() if m.expiry_ts < cutoff]
        for s in expired:
            m = self.markets.pop(s)
            self.token_to_slug.pop(m.yes_token, None)
            self.token_to_slug.pop(m.no_token, None)

    # ---------------- websocket ----------------

    async def ws_subscribe(self, ws, token_ids):
        if not token_ids:
            return
        msg = {"assets_ids": list(token_ids), "type": "market", "custom_feature_enabled": True}
        await ws.send(json.dumps(msg))

    async def run_websocket(self):
        backoff = 1
        while True:
            try:
                async with websockets.connect(self.cfg.ws_url, ping_interval=None) as ws:
                    log.info("WebSocket connected.")
                    backoff = 1
                    self.discover_and_track()
                    all_tokens = set(self.token_to_slug.keys())
                    await self.ws_subscribe(ws, all_tokens)
                    self.subscribed_tokens = all_tokens

                    last_ping = time.time()
                    last_discover = time.time()

                    while True:
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=5)
                            self.handle_ws_message(raw)
                        except asyncio.TimeoutError:
                            pass

                        now = time.time()
                        if now - last_ping >= 10:
                            await ws.send("PING")
                            last_ping = now

                        if now - last_discover >= 15:
                            self.discover_and_track()
                            self.prune_expired()
                            new_tokens = set(self.token_to_slug.keys()) - self.subscribed_tokens
                            if new_tokens:
                                await self.ws_subscribe(ws, new_tokens)
                                self.subscribed_tokens |= new_tokens
                                log.info(f"Subscribed to {len(new_tokens)} new token(s).")
                            last_discover = now

                        self.check_signals()

            except Exception as e:
                log.warning(f"WebSocket error/disconnect: {e}. Reconnecting in {backoff}s...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def handle_ws_message(self, raw: str):
        if raw in ("PONG", "pong"):
            return
        try:
            data = json.loads(raw)
        except Exception:
            return
        events = data if isinstance(data, list) else [data]
        for ev in events:
            et = ev.get("event_type") or ev.get("type")
            if et == "price_change":
                for pc in ev.get("price_changes", []):
                    self._apply_quote(pc.get("asset_id"), pc.get("best_bid"), pc.get("best_ask"))
            elif et == "best_bid_ask":
                self._apply_quote(ev.get("asset_id"), ev.get("best_bid"), ev.get("best_ask"))

    def _apply_quote(self, asset_id, bid, ask):
        if asset_id is None or bid is None or ask is None:
            return
        slug = self.token_to_slug.get(asset_id)
        if not slug:
            return
        m = self.markets.get(slug)
        if not m:
            return
        bid, ask = float(bid), float(ask)
        if asset_id == m.yes_token:
            m.yes_bid, m.yes_ask = bid, ask
        elif asset_id == m.no_token:
            m.no_bid, m.no_ask = bid, ask

    # ---------------- signal + execution ----------------

    def check_signals(self):
        now = time.time()
        for m in self.markets.values():
            if m.traded:
                continue
            yes_mid, no_mid = m.mids()
            if yes_mid is None:
                continue
            t_since_open = now - m.start_ts
            if t_since_open < 0:
                continue

            if m.k1_snapshot is None and abs(t_since_open - self.cfg.k1_seconds) <= self.cfg.tolerance_seconds:
                m.k1_snapshot = (yes_mid, no_mid)
                log.info(f"[{m.slug}] K1 snapshot captured at t={t_since_open:.1f}s: YES={yes_mid:.3f} NO={no_mid:.3f}")

            elif (m.k1_snapshot is not None and
                  abs(t_since_open - self.cfg.k2_seconds) <= self.cfg.tolerance_seconds):
                y1, n1 = m.k1_snapshot
                side = "YES" if (yes_mid - y1) >= (no_mid - n1) else "NO"
                entry_ask = m.yes_ask if side == "YES" else m.no_ask
                signal_strength = (yes_mid - y1) - (no_mid - n1)
                m.traded = True  # mark before executing so we never double-fire
                self.execute_trade(m, side, entry_ask, signal_strength)

            # give up on a market if we never got a clean K1 or K2 point near its window
            if t_since_open > self.cfg.k2_seconds + self.cfg.tolerance_seconds + 5 and m.k1_snapshot is None:
                m.traded = True  # stop checking; log as no-signal
                log_trade(dict(
                    timestamp_iso=datetime.now(timezone.utc).isoformat(), slug=m.slug, asset=m.asset,
                    duration_min=m.duration_min, side="", signal_strength="", entry_price="", shares="",
                    stake_usd=self.cfg.stake_usd, mode=self.mode, status="skipped_no_signal",
                    order_id="", note="no clean K1 tick within tolerance",
                ))

    def execute_trade(self, m: MarketState, side: str, entry_ask: float, signal_strength: float):
        # --- daily circuit breakers ---
        self.day_state = load_day_state()
        if self.day_state["trades"] >= self.cfg.max_trades_per_day:
            log.warning(f"[{m.slug}] Skipping trade: max_trades_per_day reached.")
            self._log_skip(m, side, entry_ask, signal_strength, "max_trades_per_day reached")
            return
        if self.day_state["realized_pnl"] <= -abs(self.cfg.max_daily_loss_usd):
            log.warning(f"[{m.slug}] Skipping trade: max_daily_loss_usd circuit breaker tripped.")
            self._log_skip(m, side, entry_ask, signal_strength, "daily loss limit tripped")
            return

        if entry_ask is None or entry_ask <= 0:
            self._log_skip(m, side, entry_ask, signal_strength, "no valid ask price")
            return

        # --- minimum order size check (this is the important one for $0.20 stakes) ---
        min_shares = self._get_min_shares(m)
        shares = self.cfg.stake_usd / entry_ask
        below_min = shares < min_shares
        min_size_test = False

        if below_min and self.cfg.skip_below_min_size:
            log.warning(
                f"[{m.slug}] SKIPPED: {shares:.3f} shares at ${entry_ask:.2f} "
                f"(stake ${self.cfg.stake_usd:.2f}) is below the {min_shares:.0f}-share minimum. "
                f"Would need ~${min_shares*entry_ask:.2f} to place this trade. "
                f"(Set SKIP_BELOW_MIN_SIZE=false to test whether FOK/FAK is actually exempt from this.)"
            )
            self._log_skip(m, side, entry_ask, signal_strength,
                            f"below min order size ({min_shares:.0f} shares = ${min_shares*entry_ask:.2f})")
            return
        elif below_min and not self.cfg.skip_below_min_size:
            # Deliberately submitting below the documented minimum to see what the
            # exchange itself says for FOK/FAK. A rejection costs nothing but an API
            # call. Result gets tagged min_size_test so it's easy to find in the log.
            min_size_test = True
            log.info(f"[{m.slug}] Below documented minimum ({shares:.3f} < {min_shares:.0f} shares) "
                      f"but SKIP_BELOW_MIN_SIZE=false — attempting anyway to test FOK/FAK behavior.")

        token_id = m.yes_token if side == "YES" else m.no_token

        test_prefix = "[MIN_SIZE_TEST] " if min_size_test else ""

        if self.mode != "live":
            log.info(f"[DRY RUN] {test_prefix}Would BUY {side} on {m.slug}: {shares:.3f} shares @ ${entry_ask:.2f} "
                      f"(stake ${self.cfg.stake_usd:.2f})")
            self._finish_trade(m, side, entry_ask, shares, signal_strength, status="dry_run",
                                order_id="", note=test_prefix.strip())
            return

        try:
            resp = self.client.create_and_post_order(
                order_args=OrderArgs(
                    token_id=token_id,
                    price=round(entry_ask, 4),
                    side=Side.BUY,
                    size=round(shares, 2),
                ),
                options=PartialCreateOrderOptions(tick_size=m.tick_size, neg_risk=m.neg_risk),
                order_type=OrderType.FOK,
            )
            order_id = resp.get("orderID", "") if isinstance(resp, dict) else ""
            success = resp.get("success", False) if isinstance(resp, dict) else False
            status = "live_order_placed" if success else "live_order_failed"
            log.info(f"[{m.slug}] {test_prefix}LIVE order response: {resp}")
            if min_size_test:
                if success:
                    log.info(f"[{m.slug}] MIN_SIZE_TEST RESULT: FILLED below the documented "
                              f"{min_shares:.0f}-share minimum — FOK/FAK appears exempt from that limit.")
                else:
                    err = resp.get("errorMsg", "") if isinstance(resp, dict) else str(resp)
                    log.info(f"[{m.slug}] MIN_SIZE_TEST RESULT: REJECTED ({err}) — the documented "
                              f"{min_shares:.0f}-share minimum does appear to apply to FOK/FAK too.")
            self._finish_trade(m, side, entry_ask, shares, signal_strength, status=status,
                                order_id=order_id, note=(test_prefix + json.dumps(resp)[:180]))
        except Exception as e:
            log.error(f"[{m.slug}] {test_prefix}Order placement FAILED: {e}")
            self._finish_trade(m, side, entry_ask, shares, signal_strength, status="error",
                                order_id="", note=(test_prefix + str(e)[:180]))

    def _get_min_shares(self, m: MarketState) -> float:
        try:
            info = self.client.get_clob_market_info(condition_id=m.condition_id)
            min_size = info.get("minimum_order_size") or info.get("min_order_size")
            if min_size:
                return float(min_size)
        except Exception:
            pass
        return self.cfg.fallback_min_shares

    def _log_skip(self, m, side, entry_ask, signal_strength, note):
        log_trade(dict(
            timestamp_iso=datetime.now(timezone.utc).isoformat(), slug=m.slug, asset=m.asset,
            duration_min=m.duration_min, side=side, signal_strength=f"{signal_strength:.4f}",
            entry_price=f"{entry_ask:.4f}" if entry_ask else "", shares="", stake_usd=self.cfg.stake_usd,
            mode=self.mode, status="skipped", order_id="", note=note,
        ))

    def _finish_trade(self, m, side, entry_ask, shares, signal_strength, status, order_id, note):
        log_trade(dict(
            timestamp_iso=datetime.now(timezone.utc).isoformat(), slug=m.slug, asset=m.asset,
            duration_min=m.duration_min, side=side, signal_strength=f"{signal_strength:.4f}",
            entry_price=f"{entry_ask:.4f}", shares=f"{shares:.3f}", stake_usd=self.cfg.stake_usd,
            mode=self.mode, status=status, order_id=order_id, note=note,
        ))
        self.day_state["trades"] += 1
        self.day_state["committed_usd"] += self.cfg.stake_usd
        save_day_state(self.day_state)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Polymarket momentum-crossover bot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Run environment/auth/geoblock checks and exit")
    group.add_argument("--dry-run", action="store_true", help="Simulate trades with live prices, no real orders")
    group.add_argument("--live", action="store_true", help="Place real orders with real money")
    args = parser.parse_args()

    mode = "check" if args.check else ("live" if args.live else "dry-run")
    bot = Bot(CFG, mode)

    print(f"\n=== Polymarket Momentum Bot — mode: {mode.upper()} ===")
    print(f"Assets: {', '.join(CFG.assets)} | Durations: {CFG.durations_min} min | "
          f"K1={CFG.k1_seconds}s K2={CFG.k2_seconds}s | Stake: ${CFG.stake_usd:.2f}/trade")
    print(f"Max trades/day: {CFG.max_trades_per_day} | Max daily loss: ${CFG.max_daily_loss_usd:.2f}")
    print(f"Trade log: {TRADE_LOG_PATH}\n")

    ok = bot.run_checks()
    if not ok:
        sys.exit("\nPre-flight checks failed — see errors above. Not starting.")

    if mode == "check":
        print("\nAll checks passed. Environment looks ready.")
        return

    if mode == "live":
        print("\n" + "!" * 70)
        print("You are about to place REAL ORDERS WITH REAL MONEY.")
        print(f"Stake per trade: ${CFG.stake_usd:.2f}  |  Max daily loss: ${CFG.max_daily_loss_usd:.2f}")
        print("This strategy has a thin statistical basis (~33-53 backtested trades).")
        print("!" * 70)

        required_phrase = "I UNDERSTAND THE RISK"
        if sys.stdin.isatty():
            # Interactive session (Termux, fly console, fly ssh console) — ask directly.
            confirm = input(f'\nType exactly "{required_phrase}" to proceed: ')
            if confirm.strip() != required_phrase:
                sys.exit("Confirmation not received. Exiting without trading.")
        else:
            # Headless deployment (Fly.io machine with no attached terminal) — input()
            # would hang or raise EOFError here, so require the same confirmation as
            # a secret instead. Still a deliberate, typed opt-in; just set once ahead
            # of time rather than typed live: `fly secrets set CONFIRM_LIVE_TRADING="I UNDERSTAND THE RISK"`
            env_confirm = os.environ.get("CONFIRM_LIVE_TRADING", "")
            if env_confirm.strip() != required_phrase:
                sys.exit(
                    "No terminal attached and CONFIRM_LIVE_TRADING is not set correctly.\n"
                    f'Set it with: fly secrets set CONFIRM_LIVE_TRADING="{required_phrase}"\n'
                    "Exiting without trading."
                )
            log.info("CONFIRM_LIVE_TRADING verified — proceeding with live trading (headless deployment).")

    try:
        asyncio.run(bot.run_websocket())
    except KeyboardInterrupt:
        print("\nStopped by user.")


if __name__ == "__main__":
    main()
