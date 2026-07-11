"""Ollama Cloud Usage Collector — scrapes ollama.com/settings for usage data."""

import re
import json
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx

logger = logging.getLogger("ollama-usage-monitor")

COOKIE_NAME = "__Secure-session"
USAGE_URL = "https://ollama.com/settings"
PLAN_TIERS = {"free": 0, "basic": 1, "pro": 2, "max": 3}

# Limits per tier (based on known Ollama Cloud limits)
TIER_LIMITS = {
    "free":   {"session": 15,   "weekly": 30},
    "basic":  {"session": 100,  "weekly": 200},
    "pro":    {"session": 2000, "weekly": 5000},
    "max":    {"session": 5000, "weekly": 10000},
}

# Default limits if tier unknown
DEFAULT_LIMITS = {"session": 2000, "weekly": 5000}


class UsageData:
    """Parsed usage data from the Ollama Cloud settings page."""

    def __init__(self, raw: dict):
        self.session_pct: float = raw.get("session_pct", 0.0)
        self.weekly_pct: float = raw.get("weekly_pct", 0.0)
        self.session_used: int = raw.get("session_used", 0)
        self.weekly_used: int = raw.get("weekly_used", 0)
        self.session_limit: int = raw.get("session_limit", 2000)
        self.weekly_limit: int = raw.get("weekly_limit", 5000)
        self.tier: str = raw.get("tier", "unknown")
        self.reset_in_min: int = raw.get("reset_in_min", 0)
        self.reset_at: str = raw.get("reset_at", "")
        self.models: list = raw.get("models", [])
        self.ts: str = raw.get("ts", datetime.now(timezone.utc).isoformat())

    @property
    def session_remaining(self) -> int:
        return max(0, self.session_limit - self.session_used)

    @property
    def weekly_remaining(self) -> int:
        return max(0, self.weekly_limit - self.weekly_used)

    def to_dict(self) -> dict:
        return {
            "session_pct": self.session_pct,
            "weekly_pct": self.weekly_pct,
            "session_used": self.session_used,
            "weekly_used": self.weekly_used,
            "session_limit": self.session_limit,
            "weekly_limit": self.weekly_limit,
            "session_remaining": self.session_remaining,
            "weekly_remaining": self.weekly_remaining,
            "tier": self.tier,
            "reset_in_min": self.reset_in_min,
            "reset_at": self.reset_at,
            "models": self.models,
            "ts": self.ts,
        }

    def __repr__(self) -> str:
        return (
            f"UsageData(tier={self.tier}, session={self.session_pct:.1f}% "
            f"({self.session_used}/{self.session_limit}), "
            f"weekly={self.weekly_pct:.1f}% ({self.weekly_used}/{self.weekly_limit}), "
            f"reset_in={self.reset_in_min}min)"
        )


class UsageCollector:
    """Fetches and parses usage data from ollama.com/settings."""

    def __init__(self, cookie: str, timeout: int = 10):
        self.cookie = cookie
        self.timeout = timeout

    def _parse_page(self, html: str) -> Optional[dict]:
        """Parse the HTML page to extract usage data."""
        data = {}

        # --- Plan tier ---
        tier_match = re.search(
            r'capitalize[^>]*>\s*(\w+)\s*</span',
            html, re.DOTALL
        )
        data["tier"] = tier_match.group(1).strip().lower() if tier_match else "unknown"

        # Set limits based on tier
        limits = TIER_LIMITS.get(data["tier"], DEFAULT_LIMITS)
        data["session_limit"] = limits["session"]
        data["weekly_limit"] = limits["weekly"]

        # --- Session usage percentage ---
        session_pct_match = re.search(
            r'Session usage.*?(\d+(?:\.\d+)?)%\s*used',
            html, re.DOTALL
        )
        if session_pct_match:
            data["session_pct"] = float(session_pct_match.group(1))
        else:
            # Try the aria-label on the track bar
            aria_match = re.search(
                r'aria-label="Session usage\s+(\d+(?:\.\d+)?)%\s*used"',
                html
            )
            data["session_pct"] = float(aria_match.group(1)) if aria_match else 0.0

        # --- Weekly usage percentage ---
        weekly_pct_match = re.search(
            r'Weekly usage.*?(\d+(?:\.\d+)?)%\s*used',
            html, re.DOTALL
        )
        data["weekly_pct"] = float(weekly_pct_match.group(1)) if weekly_pct_match else 0.0

        # --- Calculate used counts from percentages ---
        data["session_used"] = int(round(data["session_pct"] / 100.0 * data["session_limit"]))
        data["weekly_used"] = int(round(data["weekly_pct"] / 100.0 * data["weekly_limit"]))

        # --- Reset time ---
        reset_match = re.search(
            r'Resets in\s+(\d+)\s+minutes?',
            html
        )
        if reset_match:
            data["reset_in_min"] = int(reset_match.group(1))
            reset_at = datetime.now(timezone.utc) + timedelta(minutes=data["reset_in_min"])
            data["reset_at"] = reset_at.isoformat()
        else:
            data["reset_in_min"] = 0
            data["reset_at"] = ""

        # --- Model breakdown (deduplicated: sum requests per model) ---
        models_raw = {}
        for m in re.finditer(
            r'data-model="([^"]+)"[^>]*data-requests="(\d+)"',
            html
        ):
            name = m.group(1)
            reqs = int(m.group(2))
            models_raw[name] = models_raw.get(name, 0) + reqs

        total_reqs = sum(models_raw.values()) if models_raw else 1
        models = [
            {"name": name, "requests": reqs, "pct": round(reqs / total_reqs * 100, 1)}
            for name, reqs in sorted(models_raw.items(), key=lambda x: -x[1])
        ]
        data["models"] = models
        data["ts"] = datetime.now(timezone.utc).isoformat()

        return data

    def fetch(self) -> Optional[UsageData]:
        """Fetch current usage data from ollama.com."""
        try:
            resp = httpx.get(
                USAGE_URL,
                cookies={COOKIE_NAME: self.cookie},
                timeout=self.timeout,
                follow_redirects=True,
            )
            resp.raise_for_status()
        except httpx.HTTPError as e:
            logger.error("HTTP error fetching usage: %s", e)
            return None

        raw = self._parse_page(resp.text)
        if not raw:
            logger.error("Failed to parse usage page")
            return None

        logger.info("Fetched usage: session=%.1f%%, weekly=%.1f%%, tier=%s",
                     raw["session_pct"], raw["weekly_pct"], raw["tier"])
        return UsageData(raw)