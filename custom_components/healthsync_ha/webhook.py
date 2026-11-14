"""Webhook handling for the Apple HealthKit Bridge integration."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
import logging
from typing import Any

from aiohttp import web
from homeassistant.components import webhook
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from .const import (
    DOMAIN,
    DEFAULT_METRIC_UNITS,
    signal_metric_update,
    signal_new_metric,
    CONF_WEIGHT_UNIT,
    CONF_DISTANCE_UNIT,
    CONF_TEMPERATURE_UNIT,
    CONF_ENERGY_UNIT,
    CONF_HYDRATION_UNIT,
)
from .validation import validate_payload, sanitize_payload
from .normalize import expand

MAX_HISTORY = 10
RATE_LIMIT_REQUESTS = 1200  # Max requests per window
RATE_LIMIT_WINDOW = timedelta(minutes=1)  # Time window for rate limiting
_LOGGER = logging.getLogger(__name__)


@dataclass
class MetricState:
    """State container for a HealthKit metric."""

    value: float | int | str
    unit: str
    last_updated: datetime
    source_device: str | None = None
    samples: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_HISTORY))

    def as_attributes(self) -> dict[str, Any]:
        values = list(self.samples)
        attrs: dict[str, Any] = {
            "last_updated": self.last_updated.isoformat(),
            "source_device": self.source_device,
        }
        if values:
            attrs["min"] = min(values)
            attrs["max"] = max(values)
            attrs["avg"] = sum(values) / len(values)
        return attrs


class AppleHealthManager:
    """Manage metric storage and webhook dispatch."""

    def __init__(self, hass: HomeAssistant, entry_id: str, options: dict[str, Any]) -> None:
        self.hass = hass
        self.entry_id = entry_id
        self.options = options
        self.metrics: dict[str, MetricState] = {}
        self._rate_limit_timestamps: deque[datetime] = deque(maxlen=RATE_LIMIT_REQUESTS)

    def register(self, webhook_id: str) -> None:
        """Register a webhook handler for this entry."""

        async def _handle_webhook(
            hass: HomeAssistant, _id: str, request
        ) -> web.Response:
            # Rate limiting check
            now = datetime.now(timezone.utc)
            self._rate_limit_timestamps.append(now)

            # Clean old timestamps outside the window
            cutoff = now - RATE_LIMIT_WINDOW
            while self._rate_limit_timestamps and self._rate_limit_timestamps[0] < cutoff:
                self._rate_limit_timestamps.popleft()

            # Check if rate limit exceeded
            if len(self._rate_limit_timestamps) > RATE_LIMIT_REQUESTS:
                _LOGGER.warning(
                    "Rate limit exceeded: %d requests in last %s",
                    len(self._rate_limit_timestamps),
                    RATE_LIMIT_WINDOW
                )
                return web.Response(
                    text="rate limit exceeded",
                    status=429,
                    headers={"Content-Type": "text/plain", "Retry-After": "60"},
                )

            try:
                payload = await request.json()
            except Exception as err:
                _LOGGER.warning("Webhook received non-JSON payload: %s", err)
                return web.Response(
                    text="invalid json",
                    status=400,
                    content_type="text/plain",
                )

            _LOGGER.debug("Webhook payload received: %s", payload)
            if isinstance(payload, list):
                # Validate list size to prevent DoS
                if len(payload) > 100:
                    _LOGGER.warning("Webhook payload too large: %d items, max 100", len(payload))
                    return web.Response(
                        text="payload too large",
                        status=413,
                        content_type="text/plain",
                    )
                _LOGGER.debug("Processing batch webhook items: count=%d", len(payload))
                for item in payload:
                    if isinstance(item, dict):
                        exp = expand(item)
                        for sub in (item,) if exp is None else exp:
                            self._process_payload(sub)
            elif isinstance(payload, dict):
                _LOGGER.debug(
                    "Processing single webhook metric=%s ts=%s value=%s",
                    payload.get("metric"),
                    payload.get("timestamp"),
                    payload.get("value"),
                )
                exp = expand(payload)
                for sub in (payload,) if exp is None else exp:
                    self._process_payload(sub)
            else:
                _LOGGER.warning("Webhook payload ignored (not dict/list): %s", type(payload))
            return web.Response(
                text="ok", status=200, content_type="text/plain"
            )

        webhook.async_register(
            self.hass,
            DOMAIN,
            f"{DOMAIN}_{self.entry_id}",
            webhook_id,
            _handle_webhook,
        )
        _LOGGER.info("Registered HealthSync HA webhook id %s", webhook_id)

    def unregister(self, webhook_id: str) -> None:
        """Unregister the webhook."""
        webhook.async_unregister(self.hass, webhook_id)
        _LOGGER.info("Unregistered HealthSync HA webhook id %s", webhook_id)

    def _process_payload(self, payload: dict[str, Any]) -> None:
        """Validate and dispatch incoming payload."""
        # Sanitize payload first to prevent injection attacks
        payload = sanitize_payload(payload)

        # Validate payload structure and content
        is_valid, error = validate_payload(payload)
        if not is_valid:
            _LOGGER.warning("Invalid webhook payload: %s - %s", error, payload)
            return

        metric = payload.get("metric")
        if not metric:
            _LOGGER.warning("Webhook payload missing 'metric': %s", payload)
            return
        if metric == "body_mass":
            metric = "weight"
        value = payload.get("value")
        unit = payload.get("unit") or DEFAULT_METRIC_UNITS.get(metric, "")
        timestamp = payload.get("timestamp")
        device = payload.get("device")

        value, unit = self._convert_units(metric, value, unit)
        _LOGGER.debug(
            "Metric update metric=%s value=%s unit=%s device=%s ts=%s",
            metric,
            value,
            unit,
            device,
            timestamp,
        )

        try:
            ts = (
                datetime.fromisoformat(timestamp)
                if isinstance(timestamp, str)
                else datetime.now(timezone.utc)
            )
        except (TypeError, ValueError):
            ts = datetime.now(timezone.utc)

        state = self.metrics.get(metric)
        if state is None:
            state = MetricState(value=value, unit=unit, last_updated=ts, source_device=device)
            if isinstance(value, (int, float)):
                state.samples.append(float(value))
            self.metrics[metric] = state
            async_dispatcher_send(self.hass, signal_new_metric(self.entry_id), metric)
        else:
            try:
                if ts < state.last_updated:
                    return  # older (history) sample must not overwrite a newer value
            except TypeError:
                pass
            state.value = value
            state.unit = unit
            state.last_updated = ts
            state.source_device = device or state.source_device
            if isinstance(value, (int, float)):
                state.samples.append(float(value))
        # Send metric-specific signal for efficiency (only target sensor receives it)
        async_dispatcher_send(self.hass, signal_metric_update(self.entry_id, metric))

    def _convert_units(self, metric: str, value: Any, unit: str) -> tuple[Any, str]:
        """Convert incoming value/unit to user-selected units."""
        if not isinstance(value, (int, float)):
            return value, unit
        opts = self.options or {}
        target_unit = None
        if metric in ("weight", "lean_body_mass"):
            target_unit = opts.get(CONF_WEIGHT_UNIT)
            if target_unit == "kg" and unit == "lb":
                return value * 0.45359237, "kg"
            if target_unit == "lb" and unit == "kg":
                return value / 0.45359237, "lb"
        elif metric in ("distance_walking_running", "distance_cycling"):
            target_unit = opts.get(CONF_DISTANCE_UNIT)
            if target_unit == "km" and unit == "mi":
                return value * 1.60934, "km"
            if target_unit == "mi" and unit == "km":
                return value / 1.60934, "mi"
        elif metric == "basal_body_temperature":
            target_unit = opts.get(CONF_TEMPERATURE_UNIT)
            if target_unit == "degC" and unit.lower().startswith("degf"):
                return (value - 32) / 1.8, "degC"
            if target_unit == "degF" and unit.lower().startswith("degc"):
                return (value * 1.8) + 32, "degF"
        elif metric in ("active_energy_burned", "basal_energy_burned"):
            target_unit = opts.get(CONF_ENERGY_UNIT)
            if target_unit == "kJ" and unit == "kcal":
                return value * 4.184, "kJ"
            if target_unit == "kcal" and unit == "kJ":
                return value / 4.184, "kcal"
        elif metric == "hydration":
            target_unit = opts.get(CONF_HYDRATION_UNIT)
            if target_unit == "L" and unit in ("fl oz", "fl_oz", "fl oz US"):
                return value * 0.0295735, "L"
            if target_unit == "fl_oz" and unit in ("L", "l"):
                return value / 0.0295735, "fl oz"
        return value, unit
