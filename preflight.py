"""
POLYBOT — Pre-Flight Check
Run this BEFORE starting main.py with real money.
Verifies: V2 SDK is installed, credentials authenticate, wallet
balance (pUSD) is visible, allowance is set, and at least one live
market can be discovered and subscribed to.

Usage:
    python3 preflight.py
"""
import asyncio
import sys
from config import Config


def line():
    print("-" * 56)


async def main():
    print("=" * 56)
    print("  POLYBOT PRE-FLIGHT CHECK")
    print("=" * 56)

    failures = []

    # ── 0. SDK version check ──────────────────────────────────
    line()
    print("[0/6] Checking SDK package...")
    try:
        import py_clob_client_v2
        print("  ✓ py_clob_client_v2 is installed")
    except ImportError:
        print("  ✗ py_clob_client_v2 NOT installed.")
        print("    Polymarket migrated to CLOB V2 on 2026-04-28.")
        print("    The old py-clob-client package is archived and")
        print("    no longer works against production.")
        print("    Fix: pip install py-clob-client-v2")
        failures.append("V2 SDK not installed")
        line()
        print("✗ STOPPING — install the correct SDK first")
        sys.exit(1)

    from core.order_executor import OrderExecutor
    from core.market_discovery import MarketDiscovery

    # ── 1. Config sanity ─────────────────────────────────────
    line()
    print("[1/6] Checking config.py / .env values...")
    if not Config.PRIVATE_KEY or Config.PRIVATE_KEY.startswith("your_"):
        print("  ✗ PRIVATE_KEY is missing or still a placeholder")
        failures.append("PRIVATE_KEY not set")
    else:
        print(f"  ✓ PRIVATE_KEY present ({len(Config.PRIVATE_KEY)} chars)")

    sig_names = {
        0: "EOA",
        1: "POLY_PROXY (Email/Magic)",
        2: "POLY_GNOSIS_SAFE (Browser wallet)",
        3: "POLY_1271 (Deposit wallet)",
    }
    sig_type = Config.WALLET_SIGNATURE_TYPE
    print(f"  ℹ WALLET_SIGNATURE_TYPE = {sig_type} "
          f"({sig_names.get(sig_type, 'unknown')})")

    # FUNDER_ADDRESS is only required for types 1/2/3 — type 0
    # (EOA) doesn't use it at all, so don't fail the check for a
    # blank value there.
    funder_blank = (not Config.FUNDER_ADDRESS or
                     Config.FUNDER_ADDRESS.startswith("your_"))
    if sig_type == 0:
        if funder_blank:
            print("  ✓ FUNDER_ADDRESS not needed for EOA (type 0) — OK blank")
        else:
            print(f"  ℹ FUNDER_ADDRESS set but unused for type 0: "
                  f"{Config.FUNDER_ADDRESS[:10]}...")
    else:
        if funder_blank:
            print(f"  ✗ FUNDER_ADDRESS is required for type {sig_type} "
                  f"({sig_names.get(sig_type)}) but is missing or still "
                  f"a placeholder")
            failures.append("FUNDER_ADDRESS not set")
        else:
            print(f"  ✓ FUNDER_ADDRESS present "
                  f"({Config.FUNDER_ADDRESS[:10]}...)")

    if sig_type == 3:
        print("  ⚠ WARNING: signature_type=3 (POLY_1271 / deposit wallet)")
        print("    has a confirmed, still-open bug in py_clob_client_v2")
        print("    as of June 2026 (upstream issues #70, #75) — L1 auth")
        print("    always binds the API key to your EOA instead of the")
        print("    deposit wallet, so every order is rejected with")
        print("    'the order signer address has to be the address of")
        print("    the API KEY', regardless of correct setup. If you")
        print("    can, use signature_type=2 instead as a working")
        print("    alternative until Polymarket ships a fix.")

    if failures:
        line()
        print("✗ STOPPING — fix .env before continuing")
        sys.exit(1)

    # ── 2. Authentication ────────────────────────────────────
    line()
    print("[2/6] Authenticating with Polymarket CLOB V2...")
    executor = OrderExecutor(Config.PRIVATE_KEY, Config.FUNDER_ADDRESS)
    if not executor.auth_ok:
        print("  ✗ Authentication FAILED.")
        print("    Common causes:")
        print("    - Wrong WALLET_SIGNATURE_TYPE for your account type")
        print("    - PRIVATE_KEY doesn't match FUNDER_ADDRESS")
        print("    - Network/firewall blocking clob.polymarket.com")
        print("    - If you've triple-checked the above: as of mid-2026")
        print("      there are still open, unresolved GitHub issues")
        print("      where V1-migrated accounts (older Safe/proxy")
        print("      wallets) get rejected on every signature_type.")
        print("      Check github.com/Polymarket/py-clob-client-v2/")
        print("      issues for your exact error before assuming this")
        print("      bot is misconfigured.")
        failures.append("auth failed")
    else:
        print("  ✓ Authenticated successfully")
        if executor.api_key:
            print(f"    Derived API key: {executor.api_key[:8]}...")
            print(f"    (full key mirrored to data/derived_api_creds.json "
                  f"for reference — secret is never stored there)")

    if failures:
        line()
        print("✗ STOPPING — cannot proceed without authentication")
        sys.exit(1)

    # ── 3. Wallet balance ─────────────────────────────────────
    line()
    print("[3/6] Fetching wallet pUSD balance...")
    balance = await executor.get_wallet_balance()
    if balance <= 0:
        print(f"  ⚠ Balance reads as ${balance:.2f}")
        print("    Polymarket's collateral token is now pUSD (not")
        print("    USDC.e). If you funded with USDC.e and haven't")
        print("    wrapped it, your CLOB buying power will read $0.")
        print("    Website users: wrapping is automatic.")
        print("    API-only traders: call CollateralOnramp.wrap()")
        print("    yourself — see README Pre-Flight Checklist.")
        failures.append("zero balance")
    else:
        print(f"  ✓ Balance: ${balance:.2f} pUSD")

    # ── 4. Allowance check (critical for EOA wallets) ─────────
    line()
    print("[4/6] Checking pUSD spending allowance...")
    allowance_check = await executor.check_allowance()
    if not allowance_check["ok"]:
        print(f"  ✗ {allowance_check['reason']}")
        if Config.WALLET_SIGNATURE_TYPE == 0:
            print("    You are on signature_type=0 (EOA) — you MUST")
            print("    approve pUSD spending directly via your wallet")
            print("    before trading.")
        failures.append("allowance not set")
    else:
        print(f"  ✓ Allowance set ({allowance_check['allowance']})")

    # ── 5. Market discovery ────────────────────────────────────
    line()
    print("[5/6] Testing market discovery (Gamma API)...")
    discovery = MarketDiscovery()
    try:
        raw = await asyncio.to_thread(discovery._fetch_active_markets)
        print(f"  ✓ Gamma API reachable — {len(raw)} active crypto "
              f"markets returned")

        found_pairs = set()
        for m in raw:
            parsed = discovery._parse_market(m)
            if parsed:
                found_pairs.add(parsed["pair_id"])

        if found_pairs:
            print(f"  ✓ Matched {len(found_pairs)} of your "
                  f"{len(Config.ACTIVE_PAIRS)} configured pairs:")
            print(f"    {sorted(found_pairs)}")
            missing = set(Config.ACTIVE_PAIRS) - found_pairs
            if missing:
                print(f"  ⚠ Not currently live (may open soon): "
                      f"{sorted(missing)}")
        else:
            print("  ⚠ No markets matched your slug patterns right now.")
            print("    This can be normal if markets are between cycles.")
            print("    Re-run this check in 30 seconds.")

    except Exception as e:
        print(f"  ✗ Discovery failed: {e}")
        failures.append("discovery failed")

    # ── 6. Order signing latency check ─────────────────────────
    line()
    print("[6/6] Note on order signing latency...")
    print("  ℹ Python order signing in py_clob_client_v2 takes")
    print("    roughly ~1 second per order in current benchmarks.")
    print("    This is a known SDK characteristic, not a bug in")
    print("    this bot. It affects how tight your edge threshold")
    print("    needs to be — see README 'Execution Latency' section.")

    # ── Summary ─────────────────────────────────────────────────
    line()
    print("=" * 56)
    if failures:
        print(f"  RESULT: {len(failures)} issue(s) found — DO NOT go live")
        for f in failures:
            print(f"    - {f}")
        print("=" * 56)
        sys.exit(1)
    else:
        print("  RESULT: All checks passed ✓")
        print("  You are ready to run: python3 main.py")
        print("  (Recommend starting with UNIT_SIZE=1.0 and watching")
        print("   the first 10-20 trades closely)")
        print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
