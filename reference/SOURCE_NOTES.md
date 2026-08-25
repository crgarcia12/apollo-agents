# Original AGC Source Code Used as Reference

This demo is grounded in the real, publicly released Apollo Guidance Computer
(AGC) source code, digitized by the Virtual AGC project and published by
Chris Garry at:

https://github.com/chrislgarry/Apollo-11

The code is public domain (transcribed from hardcopy listings held by the
MIT Museum). We vendored one file directly for reference:

## `ALARM_AND_ABORT.agc` (Luminary099 — the exact build flown on Apollo 11's LM)

This is the actual assembly module (pages 1381–1385 of the Luminary 1A / 099
listing) that implements the alarm-handling primitives invoked when the
executive raised alarm codes **1202** and **1201** during the powered
descent:

- `ALARM` / `ALARM2` — turns on the DSKY "PROG" alarm light and records the
  alarm code into `FAILREG` without interrupting the running program. This is
  what let the 1202 alarm surface on the DSKY as `VERB 05 NOUN 09` while
  P63/P64 kept running underneath.
- `BAILOUT` / `POODOO` — the "give up gracefully" entry points used by the
  executive when a job could not be scheduled. These reset flags
  (`STATEFLG`, `REINTFLG`, `NODOFLAG`) and restart from a safe checkpoint
  instead of crashing — the mechanism that let the AGC recover automatically
  from the executive/core-set overflow within about a second, in time for
  the next guidance cycle.
- `PRIOLARM` / `VARALARM` — display and priority-alarm helpers that route the
  alarm code to `V05N09` (Verb 05 Noun 09 = "monitor/display alarm codes")
  on the DSKY.

Other modules in the same repository that are directly relevant to the
incident (not vendored here, but worth reading in the upstream repo):

- `Luminary099/EXECUTIVE.agc` — the job scheduler ("the executive") that
  tracks up to 8 concurrent jobs using a fixed pool of "core sets"
  (memory buffers for job state). When the rendezvous radar, left in
  AUTO/SLEW, flooded the AGC with spurious `RUPT` counter-driven jobs, the
  executive ran out of core sets — this is the literal "1202 EXECUTIVE
  OVERFLOW — NO VAC AREAS" condition.
- `Luminary099/WAITLIST.agc` — the timer-driven task list; interacts with
  the executive for scheduling periodic jobs (e.g., radar processing).
- `Luminary099/RESTARTS.agc` and `Luminary099/T4RUPT.agc` — the restart /
  priority-interrupt handling that made the recovery "graceful" instead of
  fatal, embodying Margaret Hamilton's team's asynchronous, priority-based
  scheduling design.

## Why this matters for the demo

The simulator in `../simulator/agc_simulator.py` models the same conceptual
pipeline the real AGC used:

1. A fixed-size executive job table (here simplified to a counter of "core
   sets in use", modeled after `EXECUTIVE.agc`'s job list).
2. A rendezvous-radar interrupt generator that, when "AUTO/SLEW" is
   asserted, injects extra job requests — mirroring the real hardware bug
   (a wiring/timing mismatch meant the radar's CDU kept requesting service
   even though its data wasn't needed for landing).
3. An alarm path that raises `1202`/`1201` once the core-set counter is
   exhausted, then performs the same "shed low-priority jobs and restart"
   recovery as `BAILOUT`/`POODOO`, instead of aborting the landing.

This is a simulation for education/demo purposes — it is not a cycle-accurate
AGC emulator (for that, see the excellent `yaAGC`/`Virtual AGC` project at
https://ibiblio.org/apollo/ which can actually execute Luminary099).
