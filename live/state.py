"""Persisted bot state — survives process restarts.

JSON-on-disk because it's trivial to inspect and audit. The state file is
human-readable so you can sanity-check positions and equity history without
loading the bot.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

import pandas as pd

DEFAULT_STATE_PATH = Path(__file__).resolve().parent.parent / "data" / "live_state.json"


@dataclass
class EquityPoint:
    date: str  # ISO date
    equity_usd: float
    note: str = ""


@dataclass
class BotState:
    cash_usd: float = 100_000.0
    positions: dict[str, float] = field(default_factory=dict)
    equity_history: list[EquityPoint] = field(default_factory=list)
    kill_switch_tripped: bool = False
    last_run_date: str | None = None

    @classmethod
    def load(cls, path: Path = DEFAULT_STATE_PATH) -> "BotState":
        if not path.exists():
            return cls()
        data = json.loads(path.read_text())
        eq = [EquityPoint(**pt) for pt in data.pop("equity_history", [])]
        state = cls(**data, equity_history=eq)
        return state

    def save(self, path: Path = DEFAULT_STATE_PATH) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = asdict(self)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))

    def record_equity(self, date: pd.Timestamp, equity_usd: float, note: str = "") -> None:
        self.equity_history.append(EquityPoint(date=str(pd.Timestamp(date).date()), equity_usd=equity_usd, note=note))
        self.last_run_date = str(pd.Timestamp(date).date())

    def current_drawdown(self) -> float:
        if not self.equity_history:
            return 0.0
        equities = [pt.equity_usd for pt in self.equity_history]
        peak = max(equities)
        return equities[-1] / peak - 1.0 if peak > 0 else 0.0
