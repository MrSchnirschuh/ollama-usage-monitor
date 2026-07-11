"""Analyzer — predicts if usage will hit the session limit before reset."""

import logging
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from collector import UsageData

logger = logging.getLogger("ollama-usage-monitor")

THRESHOLDS = [50, 75, 90, 95]
HISTORY_DEFAULT = Path("/data/usage_history.json")


class AnalysisResult:
    """Result of usage analysis with prediction."""

    def __init__(self, current: UsageData, history: list):
        self.current = current
        self.history = history
        self.burn_rate_per_hour: float = 0.0
        self.session_will_hit_limit: bool = False
        self.session_time_to_limit_min: Optional[float] = None
        self.weekly_will_hit_limit: bool = False
        self.alerts: list[str] = []
        self._analyze()

    def _analyze(self):
        u = self.current

        # --- Burn rate from history (last 60 min window) ---
        if len(self.history) >= 2:
            now = datetime.now(timezone.utc)
            cutoff = now - timedelta(minutes=60)
            recent = []
            for h in self.history:
                try:
                    h_ts = datetime.fromisoformat(h["ts"])
                    if h_ts >= cutoff:
                        recent.append(h)
                except (ValueError, KeyError, TypeError):
                    continue
            recent = recent[-10:]

            if len(recent) >= 2:
                first = recent[0]
                last = recent[-1]
                try:
                    t_first = datetime.fromisoformat(first["ts"])
                    t_last = datetime.fromisoformat(last["ts"])
                    elapsed_h = (t_last - t_first).total_seconds() / 3600.0
                    if elapsed_h > 0:
                        pct_change = last["session_pct"] - first["session_pct"]
                        self.burn_rate_per_hour = pct_change / elapsed_h
                except (ValueError, KeyError, TypeError):
                    pass

        # --- Prediction: session limit before reset? ---
        if self.burn_rate_per_hour > 0 and u.reset_in_min > 0:
            remaining_pct = 100.0 - u.session_pct
            time_to_limit_h = remaining_pct / self.burn_rate_per_hour
            self.session_time_to_limit_min = time_to_limit_h * 60.0
            self.session_will_hit_limit = (
                self.session_time_to_limit_min < u.reset_in_min
            )

        # --- Weekly prediction (only if multiple sessions consumed) ---
        # Weekly limit only matters if you've been through >=1 full session
        if self.burn_rate_per_hour > 0 and u.weekly_used > u.session_limit:
            weekly_remaining_pct = 100.0 - u.weekly_pct
            hours_to_weekly_limit = weekly_remaining_pct / self.burn_rate_per_hour
            self.weekly_will_hit_limit = hours_to_weekly_limit < (7 * 24)

        # --- Threshold alerts ---
        for t in THRESHOLDS:
            if u.session_pct >= t:
                self.alerts.append(f"session_{t}")

    def should_notify(self) -> bool:
        return (
            self.session_will_hit_limit
            or self.weekly_will_hit_limit
            or len(self.alerts) > 0
        )

    def to_dict(self) -> dict:
        return {
            "current": self.current.to_dict(),
            "burn_rate_per_hour": round(self.burn_rate_per_hour, 2),
            "session_will_hit_limit": self.session_will_hit_limit,
            "session_time_to_limit_min": (
                round(self.session_time_to_limit_min, 1)
                if self.session_time_to_limit_min is not None
                else None
            ),
            "weekly_will_hit_limit": self.weekly_will_hit_limit,
            "alerts": self.alerts,
            "should_notify": self.should_notify(),
            "recommendation": self.recommendation(),
        }

    def recommendation(self) -> str:
        if self.session_will_hit_limit:
            return "switch_to_flash"
        if self.weekly_will_hit_limit:
            return "reduce_usage"
        if self.current.session_pct >= 90:
            return "monitor_closely"
        if self.current.session_pct >= 75:
            return "consider_switch"
        return "ok"

    def _recommendation(self) -> str:
        return self.recommendation()

    def __repr__(self) -> str:
        ttl = (
            f"{self.session_time_to_limit_min:.0f}min"
            if self.session_time_to_limit_min is not None
            else "N/A"
        )
        return (
            f"Analysis(burn={self.burn_rate_per_hour:.1f}%/h, "
            f"will_hit_session={self.session_will_hit_limit}, "
            f"time_to_limit={ttl}, "
            f"recommendation={self.recommendation()})"
        )


class UsageAnalyzer:
    """Manages history and runs analysis."""

    def __init__(self, history_path: str = None, max_history: int = 100):
        self.history_path = Path(history_path or HISTORY_DEFAULT)
        self.max_history = max_history
        self._history: list = self._load_history()
        self._last_thresholds: set[str] = set()

    def _load_history(self) -> list:
        if self.history_path.exists():
            try:
                data = json.loads(self.history_path.read_text())
                if isinstance(data, list):
                    return data
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load history, starting fresh")
        return []

    def _save_history(self):
        self.history_path.parent.mkdir(parents=True, exist_ok=True)
        self.history_path.write_text(json.dumps(self._history, indent=2))

    def add_snapshot(self, usage: UsageData):
        self._history.append({
            "ts": usage.ts,
            "session_pct": usage.session_pct,
            "weekly_pct": usage.weekly_pct,
            "session_used": usage.session_used,
            "weekly_used": usage.weekly_used,
            "reset_in_min": usage.reset_in_min,
        })
        if len(self._history) > self.max_history:
            self._history = self._history[-self.max_history:]
        self._save_history()

    def detect_reset(self, usage: UsageData) -> bool:
        if len(self._history) < 2:
            return False
        last = self._history[-2]
        if last["session_pct"] > 30 and usage.session_pct < 10:
            logger.info("Session reset detected: %s -> %s",
                        last["session_pct"], usage.session_pct)
            self._history = []
            self._save_history()
            return True
        return False

    def analyze(self, usage: UsageData) -> AnalysisResult:
        if self.detect_reset(usage):
            self.add_snapshot(usage)
            return AnalysisResult(usage, [])

        self.add_snapshot(usage)
        result = AnalysisResult(usage, self._history)

        for alert in result.alerts[:]:
            if alert in self._last_thresholds:
                result.alerts.remove(alert)

        self._last_thresholds.update(result.alerts)
        return result

    def get_history(self) -> list:
        return self._history[-20:]