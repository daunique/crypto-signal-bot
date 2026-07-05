"""
POLYBOT — Runtime Settings
Small JSON-file-backed store for settings that can be changed
LIVE from the dashboard without restarting the bot (unlike
config.py, which requires a restart to take effect).

Currently holds: DURATION_MODE (5MIN / 15MIN / BOTH toggle).
Designed to be extended with more live-toggleable settings later
without changing the calling convention.
"""
import json
import os
import time
from config import Config

SETTINGS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "runtime_settings.json"
)

VALID_DURATION_MODES = ("BOTH", "5MIN", "15MIN")


class RuntimeSettings:
    def __init__(self, path: str = SETTINGS_PATH):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.exists(self.path):
            try:
                with open(self.path, "r") as f:
                    data = json.load(f)
                if data.get("duration_mode") in VALID_DURATION_MODES:
                    return data
            except Exception as e:
                print(f"[SETTINGS] Failed to load runtime_settings.json, "
                      f"using config.py default: {e}")
        # No file yet, or it was invalid — fall back to config.py default
        return {
            "duration_mode": Config.DURATION_MODE,
            "updated_at": time.time(),
        }

    def _save(self):
        try:
            with open(self.path, "w") as f:
                json.dump(self._data, f, indent=2)
        except Exception as e:
            print(f"[SETTINGS] Failed to persist runtime_settings.json: {e}")

    @property
    def duration_mode(self) -> str:
        return self._data.get("duration_mode", "BOTH")

    def set_duration_mode(self, mode: str) -> bool:
        """Returns True if the change was applied, False if invalid."""
        mode = mode.upper()
        if mode not in VALID_DURATION_MODES:
            return False
        old = self._data.get("duration_mode")
        self._data["duration_mode"] = mode
        self._data["updated_at"] = time.time()
        self._save()
        print(f"[SETTINGS] duration_mode changed: {old} → {mode}")
        return True

    def is_pair_live(self, pair_id: str) -> bool:
        """
        Whether a pair should be LIVE-TRADED right now, given the
        current toggle. Discovery still tracks every pair regardless
        (for comparison stats) — this only gates actual order
        execution in websocket_listener.py.
        """
        mode = self.duration_mode
        if mode == "BOTH":
            return True
        if mode == "5MIN":
            return pair_id.endswith("_5MIN")
        if mode == "15MIN":
            return pair_id.endswith("_15MIN")
        return True  # Fail-open to BOTH rather than silently trading nothing
