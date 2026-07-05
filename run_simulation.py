"""
POLYBOT — Simulation Mode Entry Point
Run this to answer "what would $X have earned?" using REAL live
market prices, with ZERO real orders placed and NO wallet, private
key, or Polymarket account needed at all.

Usage:
    python3 run_simulation.py                  # $50 default
    python3 run_simulation.py --balance 100     # custom starting balance

This is intentionally separate from main.py. main.py requires real
credentials and places real orders — running it with an unfunded
wallet does NOT give a safe projection (see the module docstring
in core/simulation_engine.py for exactly why). This script never
constructs an OrderExecutor, never reads PRIVATE_KEY, and never
sends anything to Polymarket's order endpoints — only the public,
read-only Gamma API (market discovery) and CLOB WebSocket (price
feed), the same read-only data anyone can access without an account.

HONEST LIMITS — read before trusting the numbers:
- Assumes every simulated order fills at the observed price. Real
  execution can fail to fill; this cannot model that, so results
  are a BEST CASE, not a promise of real performance.
- Reflects only whatever period you let it run for. A few hours is
  a weak sample — see this bot's own README changelog for how
  much day-to-day variance real trade data showed. Let it run
  longer (ideally multiple days, across different times) before
  treating any number here as a real expectation.
"""
import argparse
import asyncio
import threading
from config import Config
from core.market_discovery import MarketDiscovery
from core.simulation_engine import SimulationEngine
from core.simulation_listener import SimulationListener
from dashboard.app import create_app


async def main(starting_balance: float, dashboard: bool):
    print("=" * 55)
    print("  POLYBOT — SIMULATION MODE")
    print("  No wallet. No orders. Real live prices only.")
    print("=" * 55)
    print(f"[SIM] Starting balance: ${starting_balance:.2f}")

    discovery = MarketDiscovery()
    sim = SimulationEngine(starting_balance=starting_balance)
    listener = SimulationListener(discovery=discovery, sim_engine=sim)
    discovery.listener = listener

    if dashboard:
        flask_app = create_app(discovery=discovery, sim_engine=sim)
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
        print(f"[SIM] Dashboard: http://YOUR_HOST:{Config.DASHBOARD_PORT}")

    print("[SIM] Press Ctrl+C to stop and see the final summary\n")

    try:
        await asyncio.gather(
            discovery.run_discovery_loop(listener),
            listener.run(),
        )
    except KeyboardInterrupt:
        pass
    finally:
        print("\n" + "=" * 55)
        print("  SIMULATION SUMMARY")
        print("=" * 55)
        summary = sim.get_summary()
        for k, v in summary.items():
            print(f"  {k}: {v}")
        print("=" * 55)
        print("Reminder: this reflects a BEST CASE against the period")
        print("you just ran, assuming every simulated order would have")
        print("filled at the observed price. Real fills can fail; this")
        print("cannot model that. Longer runs give a more trustworthy")
        print("picture than a short one.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="POLYBOT simulation mode")
    parser.add_argument(
        "--balance", type=float, default=50.0,
        help="Starting simulated balance in dollars (default: 50.0)"
    )
    parser.add_argument(
        "--no-dashboard", action="store_true",
        help="Disable the web dashboard, terminal output only"
    )
    args = parser.parse_args()
    asyncio.run(main(args.balance, dashboard=not args.no_dashboard))
