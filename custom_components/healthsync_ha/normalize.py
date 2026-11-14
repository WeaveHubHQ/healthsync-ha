"""Translate the HealthSync iOS app payload format ({"samples":[...]}) to the flat format used by the integration."""
from __future__ import annotations

import re
from datetime import datetime
from typing import Any

# Cumulative metrics: only daily_total samples are meaningful (others are partial intervals)
CUMULATIVE = {
    "steps": "steps_daily_total",
    "activeCalories": "active_energy_burned_daily_total",
    "restingEnergy": "basal_energy_burned_daily_total",
    "distanceWalkingRunning": "distance_walking_running_daily_total",
    "flightsClimbed": "flights_climbed_daily_total",
    "exerciseTime": "exercise_time_daily_total",
}
# Point-in-time measurements
DIRECT = {
    "heartRate": "heart_rate",
    "restingHeartRate": "resting_heart_rate",
    "heartRateVariability": "heart_rate_variability_sdnn",
    "respiratoryRate": "respiratory_rate",
    "bodyMassIndex": "bmi",
    "weight": "weight",
    "vo2Max": "vo2_max",
}
UNIT_FIX = {"count/min": "bpm"}
SLEEP_STAGE = {"asleepCore": "core", "asleepDeep": "deep", "asleepREM": "rem", "asleepUnspecified": "asleep", "inBed": "in_bed", "awake": "awake", "asleep": "asleep"}
WORKOUT_TYPE = {"functionalStrengthTraining": "functional_strength",
                "traditionalStrengthTraining": "strength_training"}


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _parse(ts: Any):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


def normalize_sample(s: dict[str, Any]) -> list[dict[str, Any]]:
    """Return 0..n flat payloads for a single sample from the app."""
    metric = s.get("metric")
    if not isinstance(metric, str):
        return []
    value = s.get("value")
    unit = s.get("unit")
    ts = s.get("end_date") or s.get("start_date")
    base: dict[str, Any] = {"timestamp": ts}
    if s.get("source"):
        base["device"] = s["source"]

    def out(m, v, u):
        d = dict(base)
        d.update(metric=m, value=v, unit=u)
        return d

    if metric == "workouts":
        wt = s.get("workout_type") or "workout"
        t = WORKOUT_TYPE.get(wt) or _snake(wt)
        res = []
        if isinstance(value, (int, float)):
            res.append(out(f"workout_energy_{t}", value, "kcal"))
        a, b = _parse(s.get("start_date")), _parse(s.get("end_date"))
        if a and b:
            res.append(out(f"workout_duration_{t}", (b - a).total_seconds(), "s"))
        return res

    if metric == "sleep":
        stage = s.get("sleep_stage")
        name = "sleep_asleep" if not stage else "sleep_" + SLEEP_STAGE.get(stage, _snake(stage))
        return [out(name, value, unit or "min")]

    if metric in CUMULATIVE:
        if not s.get("daily_total"):
            return []
        if metric == "distanceWalkingRunning" and unit == "m" and isinstance(value, (int, float)):
            value, unit = value / 1000.0, "km"
        return [out(CUMULATIVE[metric], value, unit)]

    name = DIRECT.get(metric) or _snake(metric)
    u = "breaths/min" if name == "respiratory_rate" else UNIT_FIX.get(unit, unit)
    return [out(name, value, u)]


def expand(payload: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Return None if this is not the app format (caller keeps the old behaviour)."""
    samples = payload.get("samples")
    if not isinstance(samples, list):
        return None
    res: list[dict[str, Any]] = []
    for s in samples:
        if isinstance(s, dict):
            res.extend(normalize_sample(s))
    return res
