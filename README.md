# Apollo 11 Guidance Computer (AGC) Demo — DSKY Simulator + Fabric Incident Debug

A demo that ingests real Apollo 11 mission data, replays it through a live
**DSKY** (Display & Keyboard) web UI, and shows how the famous **1202/1201
"executive overflow" program alarms** during the lunar landing would be
detected and root-caused today using a **Microsoft Fabric Real-Time
Intelligence Operations Agent**.

![status](https://img.shields.io/badge/type-demo-blue) ![data](https://img.shields.io/badge/data-real%20AGC%20source-orange)

---

## 1. What is the Apollo Guidance Computer's input/output model?

Researched from the Apollo Guidance Computer's actual interface (the DSKY)
and MIT/NASA documentation:

**Inputs (astronaut → AGC):**
- A keypad: digits `0–9`, `VERB`, `NOUN`, `+`/`-`, `CLR`, `PRO` (proceed),
  `KEY REL` (key release), `RSET` (reset), `ENTR` (enter).
- Commands are always a **VERB** (what action, e.g. 06 = display, 16 =
  monitor, 21 = load into R1, 25 = load decimal, 37 = change program) +
  **NOUN** (what data, e.g. 09 = alarm codes, 20 = gimbal angles, 33/68 =
  time/altitude-rate for descent, 36 = AGC clock, 43 = state vector time,
  62 = velocity for P66) pair, e.g. `V16N68` = "monitor altitude/rate during
  descent."
- Hardware inputs from spacecraft sensors also feed the AGC directly:
  IMU (gimbal angles), rendezvous/landing radar, translation/attitude
  controller, and engine/throttle commands — these aren't astronaut
  keypresses but are the telemetry this demo focuses on.

**Outputs (AGC → astronaut / spacecraft):**
- The DSKY's 5-line electroluminescent display: `PROG` (running program,
  e.g. P63/P64/P66), current `VERB`/`NOUN`, and three data registers
  `R1`/`R2`/`R3`.
- Status/warning lamps: `UPLINK ACTY`, `TEMP`, `NO ATT`, `GIMBAL LOCK`,
  `TRACKER`, `PROG` (program alarm), `KEY REL`, `OPR ERR`, `RESTART`, `STBY`.
- Alarm codes (e.g. **1202** = executive overflow / no vacant areas,
  **1201** = no core sets) — surfaced via `VERB 05 NOUN 09`.
- Engine/DAP (digital autopilot) commands sent back out to spacecraft
  hardware (thrusters, descent engine throttle).

Sources: MIT "Digital Apollo" appendix, Project Apollo/NASSP DSKY docs, and
the Apollo 11 verb/noun quick-reference card.

## 2. The incident this demo simulates

During Apollo 11's **Powered Descent (PDI → touchdown)**, the crew left the
rendezvous radar switch in **AUTO/SLEW** as an abort contingency. Its
Coupling Data Unit kept streaming position updates to the AGC even though
they weren't needed for landing, generating a flood of spurious
interrupt-driven jobs. The AGC's **executive** (job scheduler) has a fixed
pool of "core sets" (job-state buffers); it ran out, raising alarm
**1202** (`EXECUTIVE OVERFLOW — NO VAC AREAS`) three times and **1201**
(`NO CORE SETS`) once, at roughly `GET 102:38` and `102:42`. Because
Margaret Hamilton's team designed the executive to **shed low-priority
work and restart** instead of crashing, the primary landing-guidance jobs
kept running throughout, and — after Steve Bales/Jack Garman confirmed the
alarms were on the "safe" list — Armstrong and Aldrin continued to a safe
touchdown at `GET 102:45:57`.

This demo's `data/mission_timeline.json` encodes that real timeline, and
`simulator/agc_simulator.py` models the actual failure mechanism (radar
flood → core-set exhaustion → alarm → graceful restart), rather than just
replaying canned alarm flags.

## 3. Real AGC source code

`reference/ALARM_AND_ABORT.agc` is the **actual, unmodified (public domain)
Luminary099 source file** from the flown Apollo 11 Lunar Module AGC,
sourced from the Virtual AGC project's transcription, published at
[chrislgarry/Apollo-11](https://github.com/chrislgarry/Apollo-11). It
contains the real `ALARM`, `BAILOUT`, `POODOO`, and `PRIOLARM` routines that
implement exactly the alarm-and-recover behavior this demo simulates. See
`reference/SOURCE_NOTES.md` for a guided walkthrough and pointers to the
related `EXECUTIVE.agc`/`WAITLIST.agc`/`RESTARTS.agc` modules.

## 4. Project layout

```
apollo11-agc-demo/
├── data/
│   ├── mission_timeline.json      # Real GET-timestamped descent events incl. 1202/1201 alarms
│   └── telemetry_stream.ndjson    # Generated at runtime — NDJSON telemetry (Fabric ingestion feed)
├── simulator/
│   ├── agc_simulator.py           # Replays the timeline, models executive/core-set overflow, serves WebSocket
│   └── requirements.txt
├── dsky-ui/
│   ├── index.html / style.css / app.js   # Live DSKY replica UI (WebSocket client)
├── fabric/
│   ├── eventstream_schema.json           # Fabric Eventstream source/table schema
│   ├── kql/create_table_and_mapping.kql  # Eventhouse table + ingestion mapping
│   ├── kql/anomaly_detection_queries.kql # Detection + root-cause correlation queries
│   ├── operations_agent_playbook.md      # How to configure Fabric's Operations Agent for this incident
│   └── incident_investigation_transcript.md  # Sample Copilot "Investigator insights" root-cause report
└── reference/
    ├── ALARM_AND_ABORT.agc            # Real Luminary099 AGC source (public domain)
    └── SOURCE_NOTES.md
```

## 5. Running the demo locally

```powershell
cd apollo11-agc-demo\simulator
pip install -r requirements.txt
python agc_simulator.py --fast 20     # 20x real-time speed (~40s to replay the whole descent)
```

Then open `dsky-ui/index.html` in a browser (just double-click it, or serve
it with any static file server). It connects to `ws://localhost:8765` and
shows, live:

- `PROG` / `VERB` / `NOUN` and the three `R1`/`R2`/`R3` registers.
- The `PROG` (alarm) and `RESTART` lamps lighting up in sync with the real
  1202/1201 alarm timestamps.
- An **Executive / Core-Set Monitor** panel showing the job-table filling
  toward overflow whenever the radar is "in AUTO/SLEW" — visually
  demonstrating *why* the alarm happens, not just *that* it happens.
- A live event log — the same events being appended as NDJSON to
  `data/telemetry_stream.ndjson`.

## 6. Wiring into Microsoft Fabric (Operational Agent debugging)

`data/telemetry_stream.ndjson` is shaped to be pushed into a **Fabric
Eventstream** (Custom App source) → **Eventhouse** table → analyzed by a
**Fabric Real-Time Intelligence Operations Agent**. See:

- `fabric/eventstream_schema.json` — source/table field mapping.
- `fabric/kql/create_table_and_mapping.kql` — table DDL + ingestion mapping.
- `fabric/kql/anomaly_detection_queries.kql` — queries that surface the
  core-set-exhaustion pattern, including a `series_decompose_anomalies`
  query matching Fabric's built-in anomaly detection.
- `fabric/operations_agent_playbook.md` — how to configure the Operations
  Agent (trigger condition, Teams notification, Copilot Investigator
  insights, suggested remediation), matching Fabric's real
  detect → notify → explain → act workflow.
- `fabric/incident_investigation_transcript.md` — a worked example of the
  natural-language root-cause report Copilot's Investigator insights would
  produce from this data: correlating `radar_auto_slew` with
  `core_sets_used` and the `1202`/`1201` alarm codes, concluding the radar
  CDU flood was the cause, and recommending the same "GO" call Steve Bales'
  team made in 1969 — plus a forward-looking fix (don't leave the radar in
  AUTO/SLEW during descent).

This repository doesn't include live Fabric credentials, so the Fabric-side
artifacts are provided as ready-to-run KQL/config plus a realistic sample
output; wire `telemetry_stream.ndjson` into an actual Eventstream (e.g. via
the Event Hub-compatible endpoint) to see it work against a live Fabric
workspace.

## 7. Accuracy notes

This is an educational simulation, not a cycle-accurate AGC emulator. The
mission timeline and root cause are historically accurate (radar AUTO/SLEW
→ executive core-set overflow → 1202/1201 → graceful restart → safe
landing); the simulator's core-set arithmetic is a simplified model
inspired by, but not identical to, the real `EXECUTIVE.agc` job scheduler.
For a real cycle-accurate AGC that can execute the actual flown binary, see
the [Virtual AGC / yaAGC project](https://ibiblio.org/apollo/).
