"""
Apollo 11 AGC / DSKY Simulator
==============================

Replays the Apollo 11 powered-descent timeline (data/mission_timeline.json)
and models the same failure mechanism that produced the real 1202/1201
program alarms: the LM rendezvous radar, left in AUTO/SLEW as an abort
contingency, flooded the Apollo Guidance Computer's executive (job
scheduler) with spurious interrupt-driven jobs. The executive's fixed pool
of "core sets" (job buffers) ran out, and it raised alarm 1202 (or 1201),
then recovered by shedding low-priority work and restarting -- exactly what
`ALARM_AND_ABORT.agc` (see ../reference/) implements in BAILOUT/POODOO.

This script has two jobs:

1. Drive a live DSKY web UI over a WebSocket (ws://localhost:8765), so you
   can watch PROG/VERB/NOUN, R1/R2/R3 registers, and alarm lamps update in
   real time as the descent replays.
2. Emit each simulated telemetry sample as a line of NDJSON to
   data/telemetry_stream.ndjson -- the same shape of event you would push
   into a Microsoft Fabric Eventstream (custom endpoint / Event Hub) for
   ingestion into an Eventhouse (KQL) table for the Fabric Operations Agent
   to analyze. See ../fabric/ for the Fabric-side wiring.

Run:
    pip install -r requirements.txt
    python agc_simulator.py             # real-time replay (~ real timing)
    python agc_simulator.py --fast 20    # 20x speed
"""
import argparse
import asyncio
import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import websockets

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
TIMELINE_PATH = DATA_DIR / "mission_timeline.json"
STREAM_OUT_PATH = Path(os.environ.get(
    "TELEMETRY_STREAM_PATH",
    DATA_DIR / "telemetry_stream.ndjson",
))

MAX_CORE_SETS = 7          # Luminary Executive pool: seven core sets
RADAR_AUTO_JOB_RATE = 12.5 # extra spurious job requests/sec injected by CDU when radar in AUTO/SLEW
NORMAL_JOB_RATE = 3.0      # baseline landing-program job churn per second
TICK_HZ = 5                # simulated telemetry samples per second


def get_to_seconds(get_str: str) -> float:
    h, m, s = get_str.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def seconds_to_get(seconds: float) -> str:
    whole_seconds = int(seconds)
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:03d}:{minutes:02d}:{seconds:02d}"


@dataclass
class AgcState:
    program: str = "P63"
    verb: int = 16
    noun: int = 68
    r1: str = "00102"
    r2: str = "00063"
    r3: str = "+00090"
    prog_alarm: bool = False
    restart_lamp: bool = False
    uplink_acty: bool = False
    key_rel: bool = False
    stby: bool = False
    active_alarm_code: str | None = None
    core_sets_used: int = 0
    radar_auto: bool = False
    fail_log: list = field(default_factory=list)


def build_events():
    raw = json.loads(TIMELINE_PATH.read_text())
    events = []
    for e in raw["events"]:
        e = dict(e)
        e["t"] = get_to_seconds(e["get"])
        events.append(e)
    return sorted(events, key=lambda e: e["t"])


class Simulator:
    def __init__(self, speed: float, telemetry_sink=None):
        self.speed = speed
        self.telemetry_sink = telemetry_sink
        self.clients = set()
        self.latest_payload = None
        self.reset()

    def reset(self):
        self.events = build_events()
        self.t0 = self.events[0]["t"]
        self.t_end = self.events[-1]["t"]
        self.state = AgcState()
        self.radar_auto = False
        self.core_sets = 0.0
        self._alarm_cooldown = 0

    async def register(self, ws):
        self.clients.add(ws)
        try:
            if self.latest_payload is not None:
                await ws.send(json.dumps(self.latest_payload))
            async for _ in ws:
                pass
        finally:
            self.clients.discard(ws)

    async def broadcast(self, payload: dict):
        if not self.clients:
            return
        msg = json.dumps(payload)
        await asyncio.gather(*(c.send(msg) for c in list(self.clients)), return_exceptions=True)

    def next_event(self, t):
        due = [e for e in self.events if e["t"] <= t and not e.get("_fired")]
        for e in due:
            e["_fired"] = True
        return due

    def apply_event(self, e, stream_lines):
        s = self.state
        etype = e["type"]
        note = e.get("note", "")
        if etype == "program":
            s.program = e["program"]
            s.verb, s.noun = e["verb"], e["noun"]
            s.key_rel = True
        elif etype == "note" and "AUTO/SLEW" in note:
            self.radar_auto = True
        elif etype == "alarm":
            s.active_alarm_code = e["code"]
            s.prog_alarm = True
            s.verb, s.noun = e.get("verb", 5), e.get("noun", 9)
            s.r1 = f"{e['code']:>5}" if isinstance(e["code"], int) else e["code"].rjust(5, "0")
            s.fail_log.append({"get": e["get"], "code": e["code"], "note": note})
        elif etype == "restart":
            s.restart_lamp = True
            s.prog_alarm = False
            s.core_sets_used = 0
            self.core_sets = 0.0
        elif etype == "event" and "Touchdown" in note:
            s.stby = True
            self.radar_auto = False

        stream_lines.append({
            "event_time": datetime.now(timezone.utc).isoformat(),
            "mission_get": e["get"],
            "event_type": etype,
            "program": s.program,
            "code": e.get("code"),
            "note": note,
        })

    def tick_core_sets(self, dt: float):
        """Model the executive job table filling up when the radar is in AUTO/SLEW."""
        s = self.state
        rate = NORMAL_JOB_RATE + (RADAR_AUTO_JOB_RATE if self.radar_auto else 0.0)
        arrivals = rate * dt + random.uniform(-0.3, 0.3)
        service_rate = 6.0  # jobs drained per second by the executive scheduler
        self.core_sets = max(0.0, min(MAX_CORE_SETS, self.core_sets + arrivals - service_rate * dt))
        s.core_sets_used = round(self.core_sets)
        # decay restart/key-release "one-shot" lamps after a moment
        if s.restart_lamp and random.random() < 0.05:
            s.restart_lamp = False
        if s.key_rel and random.random() < 0.2:
            s.key_rel = False

    async def run(self):
        t = self.t0
        stream_file = STREAM_OUT_PATH.open("a", encoding="utf-8")
        print(f"[sim] Replaying Apollo 11 PDI->touchdown, speed={self.speed}x. "
              f"WebSocket on ws://localhost:8765 ...")
        dt_wall = 1.0 / TICK_HZ
        while t <= self.t_end + 2:
            due = self.next_event(t)
            stream_lines = []
            for e in due:
                self.apply_event(e, stream_lines)
                print(f"[{e['get']}] {e['type'].upper():8s} {e.get('note','')}")

            self.tick_core_sets(dt_wall * self.speed)

            event_time = datetime.now(timezone.utc).isoformat()
            telemetry = {
                "kind": "telemetry",
                "event_time": event_time,
                "mission_get": seconds_to_get(t),
                "sim_get_seconds": round(t, 1),
                "event_type": "telemetry_sample",
                "program": self.state.program,
                "verb": self.state.verb,
                "noun": self.state.noun,
                "r1": self.state.r1,
                "r2": self.state.r2,
                "r3": self.state.r3,
                "prog_alarm": self.state.prog_alarm,
                "restart_lamp": self.state.restart_lamp,
                "key_rel": self.state.key_rel,
                "stby": self.state.stby,
                "active_alarm_code": self.state.active_alarm_code,
                "code": self.state.active_alarm_code,
                "note": "",
                "core_sets_used": self.state.core_sets_used,
                "max_core_sets": MAX_CORE_SETS,
                "radar_auto_slew": self.radar_auto,
                "events": stream_lines,
            }

            for line in stream_lines:
                line.update({
                    "kind": "mission_event",
                    "sim_get_seconds": round(t, 1),
                    "program": self.state.program,
                    "verb": self.state.verb,
                    "noun": self.state.noun,
                    "r1": self.state.r1,
                    "r2": self.state.r2,
                    "r3": self.state.r3,
                    "prog_alarm": self.state.prog_alarm,
                    "restart_lamp": self.state.restart_lamp,
                    "key_rel": self.state.key_rel,
                    "stby": self.state.stby,
                    "active_alarm_code": self.state.active_alarm_code,
                    "core_sets_used": self.state.core_sets_used,
                    "max_core_sets": MAX_CORE_SETS,
                    "radar_auto_slew": self.radar_auto,
                })

            self.latest_payload = telemetry
            await self.broadcast(telemetry)
            if self.telemetry_sink is not None:
                for line in stream_lines:
                    await self.telemetry_sink(line)
                await self.telemetry_sink(telemetry)

            for line in stream_lines:
                stream_file.write(json.dumps(line) + "\n")
            stream_file.write(json.dumps({**telemetry, "kind": "telemetry_sample", "events": None}) + "\n")
            stream_file.flush()

            await asyncio.sleep(dt_wall)
            t += dt_wall * self.speed

        print("[sim] Replay complete: touchdown reached.")
        stream_file.close()

    async def run_forever(self):
        while True:
            await self.run()
            await asyncio.sleep(8)
            self.reset()

    async def serve(self):
        async with websockets.serve(self.register, "localhost", 8765):
            await self.run_forever()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fast", type=float, default=20.0,
                         help="Playback speed multiplier (default 20x realtime; PDI->touchdown takes ~12.5 min real time)")
    args = parser.parse_args()
    STREAM_OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    sim = Simulator(speed=args.fast)
    asyncio.run(sim.serve())


if __name__ == "__main__":
    main()
