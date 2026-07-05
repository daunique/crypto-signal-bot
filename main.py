"""
POLYMARKET BOT — Main Entry Point
Starts all systems: discovery, websocket, scheduler, dashboard
"""
import asyncio
import threading
from config import Config
from core.market_discovery import MarketDiscovery
from core.websocket_listener import WebSocketListener
from core.order_executor import OrderExecutor
from core.capital_manager import CapitalEngine, CapitalMode
from core.position_manager import PositionManager
from core.expiry_guard import ExpiryGuard
from risk.circuit_breaker import CircuitBreaker
from risk.fee_calculator import FeeCalculator
from data.trade_journal import TradeJournal
from data.portfolio_tracker import PortfolioTracker
from data.runtime_settings import RuntimeSettings
from scheduler import BotScheduler
from dashboard.app import create_app


async def main():
    print("=" * 55)
    print("   POLYBOT — Polymarket Arbitrage Bot")
    print("=" * 55)

    # ── Initialize core components ──────────────────────────
    journal   = TradeJournal()
    tracker   = PortfolioTracker(journal)
    discovery = MarketDiscovery()
    runtime_settings = RuntimeSettings()
    executor  = OrderExecutor(
        private_key=Config.PRIVATE_KEY,
        funder_address=Config.FUNDER_ADDRESS
    )

    # Hard stop if authentication failed. OrderExecutor already logs
    # the specific reason internally, but without this check main.py
    # would happily continue initializing the full pipeline (discovery,
    # WebSocket, scheduler, dashboard) and LOOK like it's running —
    # detecting edges, printing [EDGE] logs — while every single trade
    # attempt silently fails via _auth_error_result(). It would take
    # MAX_CONSECUTIVE_MISSES failed trades before the circuit breaker
    # noticed and halted, all while appearing "live" on the dashboard.
    # preflight.py catches this ahead of time, but main.py shouldn't
    # depend on someone remembering to run it first, or on credentials
    # staying valid between a preflight check and the actual run.
    if not executor.auth_ok:
        print("=" * 55)
        print("  FATAL: Authentication failed — bot cannot trade.")
        print("  Run 'python3 preflight.py' for a detailed diagnosis")
        print("  of PRIVATE_KEY / FUNDER_ADDRESS / WALLET_SIGNATURE_TYPE.")
        print("=" * 55)
        return
    capital = CapitalEngine(
        mode=CapitalMode[Config.CAPITAL_MODE],
        wallet_balance=Config.STARTING_BALANCE,
        unit_size=Config.UNIT_SIZE
    )
    positions    = PositionManager()
    expiry_guard = ExpiryGuard()
    fee_calc     = FeeCalculator()
    breaker      = CircuitBreaker(
        journal=journal,
        wallet_balance=Config.STARTING_BALANCE,
        positions=positions
    )

    # ── WebSocket listener (real-time price feed) ───────────
    listener = WebSocketListener(
        discovery=discovery,
        capital=capital,
        positions=positions,
        executor=executor,
        expiry_guard=expiry_guard,
        fee_calc=fee_calc,
        circuit_breaker=breaker,
        journal=journal,
        tracker=tracker,
        runtime_settings=runtime_settings
    )

    # ── Scheduler (background jobs) ─────────────────────────
    scheduler = BotScheduler(
        listener=listener,
        discovery=discovery,
        executor=executor,
        journal=journal,
        tracker=tracker,
        circuit_breaker=breaker,
        capital=capital,
        positions=positions
    )
    scheduler.setup_jobs()
    scheduler.start()

    # ── Dashboard (runs in separate thread) ─────────────────
    flask_app = create_app(tracker, discovery, journal,
                            capital=capital, listener=listener,
                            runtime_settings=runtime_settings,
                            breaker=breaker)
    dash_thread = threading.Thread(
        target=lambda: flask_app.run(
            host=Config.DASHBOARD_HOST,
            port=Config.DASHBOARD_PORT,
            debug=False,
            use_reloader=False
        ),
        daemon=True
    )
    dash_thread.start()
    print(f"[DASHBOARD] http://YOUR_VPS_IP:{Config.DASHBOARD_PORT}")

    print("[MAIN] All systems initialized — Bot is LIVE")
    print(f"[MAIN] Monitoring {len(Config.ACTIVE_PAIRS)} pairs: "
          f"{', '.join(Config.ACTIVE_PAIRS)}")
    print("[MAIN] Press Ctrl+C to stop")

    # ── Run discovery + websocket concurrently ───────────────
    try:
        await asyncio.gather(
            discovery.run_discovery_loop(listener),
            listener.run()
        )
    except KeyboardInterrupt:
        print("\n[MAIN] Shutting down gracefully...")
        scheduler.stop()


if __name__ == "__main__":
    asyncio.run(main())
