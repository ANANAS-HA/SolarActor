"""Agent tools: numbers only, read only the telemetry store, each returns a contracts.ToolResult.

Every tool in AGENT_TOOLS takes (store, start=None, end=None, ...) for the window [start, end).
The other public functions in the modules are data helpers (DataFrames for charts and other tools).
"""

from collections.abc import Callable

from raqeeb.contracts import ToolResult
from raqeeb.tools import alarms, metrics, peers, physics, weather_tools

AGENT_TOOLS: dict[str, Callable[..., ToolResult]] = {
    "sensor_check": weather_tools.sensor_check,
    "wind_summary": weather_tools.wind_summary,
    "tracker_check": physics.tracker_check,
    "plant_performance": metrics.plant_performance,
    "inverter_performance": metrics.inverter_performance,
    "data_gaps": metrics.data_gaps,
    "curtailment_periods": metrics.curtailment_periods,
    "peer_outliers": peers.peer_outliers,
    "flat_caps": peers.flat_caps,
    "plant_flat_cap": peers.plant_flat_cap,
    "temperature_check": peers.temperature_check,
    "alarm_summary": alarms.alarm_summary,
    "alarm_groups": alarms.alarm_groups,
    "unexpected_commands": alarms.unexpected_commands,
}
