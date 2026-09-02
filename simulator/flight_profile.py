"""
Apollo 11 powered-descent flight profile (altitude / range / velocity).

The DSKY simulator (agc_simulator.py) only modeled discrete AGC events
(program/verb/noun changes, alarms, restarts). This module adds a
continuous physical trajectory -- altitude above the surface, downrange
progress toward the landing site, and vertical/horizontal speed -- so a
2D "lander view" can be drawn alongside the DSKY.

Numbers are illustrative, rounded from publicly published Apollo 11 PDI
(Powered Descent Initiation) -> touchdown figures (NASA mission reports /
Apollo 11 Lunar Surface Journal): PDI at ~15,300 m (50,000 ft) altitude and
~1,697 m/s (5,560 ft/s) mostly-horizontal velocity, descending through the
braking phase, approach phase (high gate, ~2,300 m), landing phase
(low gate, ~150 m) to touchdown at a slow near-vertical descent rate. This
is a simplified piecewise interpolation for visualization, not a
guidance-accurate trajectory.
"""
from __future__ import annotations

from dataclasses import dataclass


def get_to_seconds(get_str: str) -> float:
    h, m, s = get_str.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)

# (get_string, altitude_m, downrange_progress[0..1], vertical_speed_mps, horizontal_speed_mps)
_KEYFRAMES_RAW = [
    ("102:33:05", 15300.0, 0.00, 1.0, 1697.0),   # PDI - braking phase begins
    ("102:34:00", 13700.0, 0.05, 4.0, 1600.0),
    ("102:36:00", 9800.0, 0.18, 12.0, 1300.0),
    ("102:38:23", 6000.0, 0.34, 25.0, 950.0),     # 1202 alarm #1
    ("102:39:56", 2300.0, 0.55, 45.0, 170.0),     # P64 approach phase, high gate
    ("102:42:18", 750.0, 0.74, 18.0, 60.0),       # 1202 alarm #2
    ("102:42:43", 430.0, 0.80, 15.0, 40.0),       # 1201 alarm
    ("102:44:58", 60.0, 0.94, 2.5, 5.0),          # P66 manual takeover, low gate
    ("102:45:40", 15.0, 0.985, 0.9, 1.5),         # low-level fuel light
    ("102:45:57", 0.0, 1.00, 0.5, 0.0),           # touchdown
]

KEYFRAMES = [
    (get_to_seconds(get), alt, prog, vs, hs)
    for get, alt, prog, vs, hs in _KEYFRAMES_RAW
]


def _smoothstep(a: float, b: float, t: float) -> float:
    if b <= a:
        return b
    x = max(0.0, min(1.0, (t - a) / (b - a)))
    return x * x * (3 - 2 * x)


@dataclass
class FlightSample:
    altitude_m: float
    downrange_progress: float
    vertical_speed_mps: float
    horizontal_speed_mps: float


def sample(t: float) -> FlightSample:
    """Interpolate the flight profile at absolute GET-seconds `t`."""
    frames = KEYFRAMES
    if t <= frames[0][0]:
        _, alt, prog, vs, hs = frames[0]
        return FlightSample(alt, prog, vs, hs)
    if t >= frames[-1][0]:
        _, alt, prog, vs, hs = frames[-1]
        return FlightSample(alt, prog, vs, hs)

    for (t0, alt0, prog0, vs0, hs0), (t1, alt1, prog1, vs1, hs1) in zip(frames, frames[1:]):
        if t0 <= t <= t1:
            f = _smoothstep(t0, t1, t)
            return FlightSample(
                altitude_m=alt0 + (alt1 - alt0) * f,
                downrange_progress=prog0 + (prog1 - prog0) * f,
                vertical_speed_mps=vs0 + (vs1 - vs0) * f,
                horizontal_speed_mps=hs0 + (hs1 - hs0) * f,
            )
    # Should not reach here given the bounds checks above.
    _, alt, prog, vs, hs = frames[-1]
    return FlightSample(alt, prog, vs, hs)
