"""
POLYBOT — Order Executor (CLOB V2)
Handles signed order placement via py-clob-client-v2.
FOK for two-legged arb, FAK (Polymarket's IOC) for directional/exit trades.

================================================================
IMPORTANT — Polymarket migrated to CLOB V2 on 2026-04-28.
The legacy py-clob-client (V1) package is archived and no longer
works against production. This module uses py_clob_client_v2.
Collateral is now pUSD, not USDC.e — see README "Pre-Flight
Checklist" for wrapping instructions if your balance reads as 0
despite holding USDC.e.
================================================================

Signature types — set WALLET_SIGNATURE_TYPE in .env to match how
you actually use Polymarket (see .env.example for full detail):
  0 = EOA             — raw private key, trading directly
  1 = POLY_PROXY      — Email / Magic Link login (most common)
  2 = POLY_GNOSIS_SAFE — Browser wallet via Polymarket "Connect Wallet"
  3 = POLY_1271       — newer deposit-wallet flow. NOT recommended
      right now: confirmed open bug in py_clob_client_v2 as of
      June 2026 (upstream issues #70 and #75) — L1 auth always
      binds the API key to the EOA instead of the deposit wallet,
      so every order is rejected with "the order signer address
      has to be the address of the API KEY" regardless of correct
      setup. Use type 2 as a working alternative if your account
      uses this flow, until Polymarket ships a fix.

Types 1/2/3 REQUIRE FUNDER_ADDRESS in .env (your Polymarket
proxy/Safe/deposit wallet address — found on your profile page,
not in your browser wallet extension). Type 0 (EOA) does not use
it, but DOES require on-chain pUSD + conditional token allowances
to be set once before trading, approved via your wallet directly
(not through the SDK). Email/Magic and browser-proxy accounts
have this handled automatically by Polymarket.

================================================================
API CREDENTIALS — you never manually generate these.
create_or_derive_api_key() below signs a message with your
PRIVATE_KEY and Polymarket's server returns credentials derived
from that signature. Called with no nonce argument, it uses the
default nonce (0), which means it's DETERMINISTIC — calling it
again on every bot restart derives the SAME credentials, not new
ones, so this is safe to call fresh on every startup.

⚠ DO NOT generate API keys through polymarket.com/settings for
signature_type=1 (POLY_PROXY / email-Magic accounts). Confirmed
upstream bug (py-clob-client issue #339): website-generated keys
for these accounts are registered under your PROXY wallet address,
but CLOB V2 validates orders by SIGNER (your EOA) address — the
two can never match, so every order returns 401 regardless of
setup. Always let this module derive credentials programmatically
(as it already does below) rather than using website-generated
keys for type 1 accounts.
================================================================
"""
import asyncio
import json
import os
import time
from py_clob_client_v2 import (
    ClobClient, ApiCreds, OrderArgs, OrderType, Side,
    PartialCreateOrderOptions, BalanceAllowanceParams, AssetType,
    OpenOrderParams,
)
from config import Config

# Where derived API credentials get mirrored for visibility/debugging
# ONLY — the bot never reads this file back in; it re-derives fresh
# from PRIVATE_KEY every startup (safe, since nonce=0 is deterministic).
# This file exists so you can SEE what key is in use without adding
# print statements, e.g. to cross-reference against Polymarket support
# or upstream GitHub issues that ask for your API key prefix.
_CREDS_DEBUG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "..", "data", "derived_api_creds.json"
)


class OrderExecutor:
    def __init__(self, private_key: str, funder_address: str = ""):
        """
        funder_address meaning depends on WALLET_SIGNATURE_TYPE:
          Type 0 (EOA): not needed — pass "" or omit entirely.
          Type 1/2/3:   REQUIRED — your Polymarket proxy/Safe/deposit
                        wallet address (see .env.example).
        """
        self.auth_ok = False
        self.api_key = None  # Populated after successful auth, for visibility
        # Needed for Data API position queries (get_data_api_positions),
        # which are separate from the CLOB order endpoints and are
        # queried by wallet address directly, not through the CLOB
        # client. For types 1/2/3 this is the same funder/proxy
        # address already required for trading. For type 0 (EOA),
        # where funder_address is otherwise unused, this field is
        # reused to mean "the EOA's own public address" — set it in
        # .env even for EOA if you want reconciliation to work; the
        # trading path itself still ignores it correctly for type 0
        # (see the funder omission logic below).
        self.positions_address = funder_address
        self._tick_size_cache: dict[str, str] = {}
        self._neg_risk_cache: dict[str, bool] = {}

        try:
            # Step 1: L1 auth — derive API credentials from wallet.
            # No nonce passed -> uses default nonce 0 -> deterministic,
            # so this is safe to re-run on every bot startup and will
            # always derive the SAME credentials rather than creating
            # new ones each time.
            l1_client = ClobClient(
                host=Config.CLOB_API,
                chain_id=Config.CHAIN_ID,
                key=private_key,
            )
            creds = l1_client.create_or_derive_api_key()
            self.api_key = self._extract_api_key(creds)
            self._mirror_creds_for_debug(creds)

            # Step 2: L2 auth — fully authenticated client.
            # For EOA (type 0), funder isn't part of the signing
            # scheme at all — pass it only when actually provided,
            # rather than an empty string, so the SDK's own default
            # handling applies rather than us guessing its behavior
            # for a blank value.
            client_kwargs = {
                "host": Config.CLOB_API,
                "chain_id": Config.CHAIN_ID,
                "key": private_key,
                "creds": creds,
                "signature_type": Config.WALLET_SIGNATURE_TYPE,
            }
            if funder_address:
                client_kwargs["funder"] = funder_address
            elif Config.WALLET_SIGNATURE_TYPE != 0:
                print("[EXECUTOR] WARNING — WALLET_SIGNATURE_TYPE="
                      f"{Config.WALLET_SIGNATURE_TYPE} requires "
                      "FUNDER_ADDRESS to be set in .env, but it's "
                      "blank. Authentication will likely fail.")

            self.client = ClobClient(**client_kwargs)
            self.auth_ok = True
            key_display = f"{self.api_key[:8]}..." if self.api_key else "unknown"
            print("[EXECUTOR] CLOB V2 client authenticated "
                  f"(signature_type={Config.WALLET_SIGNATURE_TYPE}, "
                  f"api_key={key_display})")
        except Exception as e:
            print(f"[EXECUTOR] AUTH FAILED — bot cannot trade: {e}")
            print("[EXECUTOR] Check PRIVATE_KEY, FUNDER_ADDRESS, and "
                  "WALLET_SIGNATURE_TYPE in .env. Also confirm "
                  "you're on py_clob_client_v2, not the archived V1 "
                  "package.")
            self.client = None

    def _extract_api_key(self, creds) -> str | None:
        """Pull just the api_key field out of whatever shape the
        SDK returns (dict or object), for display/logging only."""
        if creds is None:
            return None
        if isinstance(creds, dict):
            return creds.get("api_key") or creds.get("apiKey")
        return getattr(creds, "api_key", None) or getattr(creds, "apiKey", None)

    def _mirror_creds_for_debug(self, creds):
        """
        Writes the derived API key (NOT the secret/passphrase — only
        the key itself, which is safe to have visible on disk) to a
        local JSON file purely for debugging/support purposes, e.g.
        confirming which key is in use when cross-referencing a
        Polymarket support ticket or a GitHub issue that asks for
        your key prefix. The bot never reads this file back in — it
        always re-derives fresh from PRIVATE_KEY on startup (see
        module docstring on why that's safe and deterministic).
        Failure here is non-fatal; it's a convenience file, not part
        of the auth flow itself.
        """
        try:
            os.makedirs(os.path.dirname(_CREDS_DEBUG_PATH), exist_ok=True)
            with open(_CREDS_DEBUG_PATH, "w") as f:
                json.dump({
                    "api_key": self.api_key,
                    "signature_type": Config.WALLET_SIGNATURE_TYPE,
                    "derived_at": time.time(),
                    "note": "For debugging only. Secret/passphrase are "
                             "NOT stored here. Bot always re-derives "
                             "fresh from PRIVATE_KEY on startup.",
                }, f, indent=2)
        except Exception:
            pass  # Non-fatal — this file is a convenience, not required

    async def _get_market_params(self, token_id: str,
                                  condition_id: str = None) -> dict:
        """
        Fetch and cache tick_size and neg_risk for a token.
        REQUIRED on every order — omitting these causes silent
        rejections or precision errors on V2.

        get_market() takes a condition_id, not a token_id — if the
        caller doesn't have one (e.g. emergency sell-back paths),
        fall back to the documented Polymarket default of 0.01
        tick_size / non-neg-risk rather than failing the trade.
        """
        if token_id in self._tick_size_cache:
            return {
                "tick_size": self._tick_size_cache[token_id],
                "neg_risk": self._neg_risk_cache.get(token_id, False),
            }
        if not condition_id:
            return {"tick_size": "0.01", "neg_risk": False}
        try:
            market = await asyncio.to_thread(
                self.client.get_market, condition_id
            )
            tick_size = str(market.get("minimum_tick_size", "0.01"))
            neg_risk = bool(market.get("neg_risk", False))
            self._tick_size_cache[token_id] = tick_size
            self._neg_risk_cache[token_id] = neg_risk
            return {"tick_size": tick_size, "neg_risk": neg_risk}
        except Exception as e:
            print(f"[EXECUTOR] tick_size lookup failed for "
                  f"{token_id[:10]}..., defaulting to 0.01: {e}")
            return {"tick_size": "0.01", "neg_risk": False}

    async def place_fok(self, token_id: str, price: float,
                         size: float = None, shares: float = None,
                         condition_id: str = None) -> dict:
        """
        Fill-Or-Kill order. Fills completely now or cancels entirely.
        Used for both legs of the arbitrage trade.

        Pass EITHER:
          size   = DOLLAR amount to spend (shares computed as size/price)
          shares = EXACT share count (use this for two-legged arb so
                   YES and NO share counts match exactly — computing
                   shares independently per leg from a dollar amount
                   produces different share counts on each side and
                   breaks the guaranteed-profit hedge)
        """
        if not self.auth_ok:
            return self._auth_error_result()

        start = time.time()
        try:
            if shares is None:
                shares = round(size / price, 2) if price and size else 0
            else:
                shares = round(shares, 2)

            if shares <= 0:
                return self._zero_size_result(start)

            params = await self._get_market_params(token_id, condition_id)

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                side=Side.BUY,
                size=shares,
            )

            response = await asyncio.to_thread(
                self.client.create_and_post_order,
                order_args=order_args,
                options=PartialCreateOrderOptions(
                    tick_size=params["tick_size"]
                ),
                order_type=OrderType.FOK,
            )

            elapsed_ms = (time.time() - start) * 1000
            filled = self._is_filled(response)

            return {
                "filled":     filled,
                "fill_price": price if filled else None,
                "shares":     shares if filled else 0,
                "order_id":   self._get_order_id(response),
                "elapsed_ms": round(elapsed_ms, 2),
                "raw":        response,
            }

        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            print(f"[EXECUTOR] FOK error ({elapsed_ms:.0f}ms) "
                  f"on token {token_id[:10]}...: {e}")
            return {
                "filled": False, "fill_price": None, "shares": 0,
                "order_id": None, "elapsed_ms": round(elapsed_ms, 2),
                "error": str(e),
            }

    async def place_ioc(self, token_id: str, price: float,
                         size: float, side: str = "BUY",
                         condition_id: str = None) -> dict:
        """
        Immediate-or-cancel order (Polymarket calls this FAK —
        Fill-And-Kill). Fills whatever is available now, cancels
        the remainder. Used for directional entries and emergency
        exits.

        size = DOLLAR amount for BUY, or SHARE amount for SELL
        (sell-back paths pass the exact share count already held).
        """
        if not self.auth_ok:
            return self._auth_error_result()

        start = time.time()
        try:
            if side == "BUY":
                shares = round(size / price, 2) if price > 0 else 0
            else:
                # SELL: size IS the share count being liquidated
                shares = round(size, 2)

            if shares <= 0:
                return self._zero_size_result(start)

            params = await self._get_market_params(token_id, condition_id)
            order_side = Side.BUY if side == "BUY" else Side.SELL

            order_args = OrderArgs(
                token_id=token_id,
                price=round(price, 4),
                side=order_side,
                size=shares,
            )

            response = await asyncio.to_thread(
                self.client.create_and_post_order,
                order_args=order_args,
                options=PartialCreateOrderOptions(
                    tick_size=params["tick_size"]
                ),
                order_type=OrderType.FAK,
            )

            elapsed_ms = (time.time() - start) * 1000
            filled = self._is_filled(response)

            return {
                "filled":     filled,
                "fill_price": price if filled else None,
                "shares":     shares if filled else 0,
                "order_id":   self._get_order_id(response),
                "elapsed_ms": round(elapsed_ms, 2),
                "raw":        response,
            }

        except Exception as e:
            elapsed_ms = (time.time() - start) * 1000
            print(f"[EXECUTOR] FAK error ({elapsed_ms:.0f}ms) "
                  f"on token {token_id[:10]}...: {e}")
            return {
                "filled": False, "fill_price": None, "shares": 0,
                "order_id": None, "elapsed_ms": round(elapsed_ms, 2),
                "error": str(e),
            }

    def _is_filled(self, response) -> bool:
        """Normalize 'did this fill' across SDK response shapes."""
        if response is None:
            return False
        if isinstance(response, dict):
            if response.get("success") is False:
                return False
            status = response.get("status", "")
            return status in ("matched", "filled") or \
                   response.get("success") is True
        # Some V2 responses come back as objects, not dicts
        success = getattr(response, "success", None)
        status = getattr(response, "status", "")
        if success is False:
            return False
        return status in ("matched", "filled") or success is True

    def _get_order_id(self, response):
        if isinstance(response, dict):
            return response.get("orderID") or response.get("order_id")
        return getattr(response, "orderID", None) or \
               getattr(response, "order_id", None)

    def _auth_error_result(self) -> dict:
        return {
            "filled": False, "fill_price": None, "shares": 0,
            "order_id": None, "elapsed_ms": 0,
            "error": "CLOB client not authenticated — check credentials",
        }

    def _zero_size_result(self, start) -> dict:
        return {
            "filled": False, "fill_price": None, "shares": 0,
            "order_id": None,
            "elapsed_ms": round((time.time()-start)*1000, 2),
            "error": "Computed share size was zero or negative",
        }

    async def get_wallet_balance(self) -> float:
        """Fetch current pUSD (collateral) balance from wallet."""
        if not self.auth_ok:
            return 0.0
        try:
            result = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            balance = result.get("balance") if isinstance(result, dict) \
                       else getattr(result, "balance", 0)
            return float(balance or 0) / 1_000_000  # 6 decimals
        except Exception as e:
            print(f"[EXECUTOR] Balance fetch error: {e}")
            return 0.0

    async def check_allowance(self) -> dict:
        """
        Check if pUSD spending allowance is set.
        EOA wallets MUST have allowance > 0 or every order will fail
        with 'not enough balance / allowance' even when funded.
        Email/Magic and browser-proxy wallets usually have this
        configured automatically.
        """
        if not self.auth_ok:
            return {"ok": False, "reason": "not authenticated"}
        try:
            result = await asyncio.to_thread(
                self.client.get_balance_allowance,
                BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            allowance = result.get("allowance") if isinstance(result, dict) \
                        else getattr(result, "allowance", 0)
            allowance = int(allowance or 0)
            if allowance == 0:
                return {
                    "ok": False,
                    "reason": "pUSD allowance is 0. EOA wallets must "
                              "approve spending before trading — see "
                              "the README Pre-Flight Checklist. Email/"
                              "Magic and browser-proxy wallets usually "
                              "have this set automatically.",
                }
            return {"ok": True, "allowance": allowance}
        except Exception as e:
            return {"ok": False, "reason": str(e)}

    async def get_data_api_positions(self) -> list:
        """
        Fetch ACTUAL HELD POSITIONS (filled shares you currently own)
        from Polymarket's Data API — data-api.polymarket.com/positions.

        This is intentionally NOT the same as querying the CLOB's
        order endpoints (get_orders/OpenOrderParams), which only
        return RESTING limit orders (GTC/GTD) still sitting on the
        book. This bot exclusively uses FOK and FAK order types,
        both of which either fill immediately or get cancelled —
        they never rest — so a CLOB open-orders query would always
        return an empty list regardless of how many positions are
        actually held. The Data API is the correct, separate service
        for querying what you actually own.

        Returns a list of dicts like:
          {"token_id": "...", "size": 2.11, "avg_price": 0.47, ...}
        (exact shape depends on the API response; callers should
        treat unknown extra fields as opaque and only rely on
        token_id and size.)
        """
        if not self.positions_address:
            print("[EXECUTOR] Cannot fetch positions — no address set "
                  "(FUNDER_ADDRESS is required in .env for position "
                  "reconciliation, even for EOA/type 0 accounts).")
            return []
        try:
            import aiohttp
            url = f"{Config.DATA_API}/positions"
            params = {"user": self.positions_address, "sizeThreshold": "0.01"}
            async with aiohttp.ClientSession() as session:
                async with session.get(url, params=params, timeout=10) as resp:
                    if resp.status != 200:
                        print(f"[EXECUTOR] Data API positions returned "
                              f"HTTP {resp.status}")
                        return []
                    data = await resp.json()
                    return data if isinstance(data, list) else []
        except Exception as e:
            print(f"[EXECUTOR] Data API positions fetch error: {e}")
            return []

    async def cancel_all_orders(self):
        """Emergency: cancel all open orders (kill switch action)."""
        if not self.auth_ok:
            return
        try:
            await asyncio.to_thread(self.client.cancel_all)
            print("[EXECUTOR] All open orders cancelled")
        except Exception as e:
            print(f"[EXECUTOR] Cancel-all error: {e}")
