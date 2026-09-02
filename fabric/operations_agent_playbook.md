# Microsoft Fabric Operations Agent — Configuration Playbook

This describes how the incident in this demo (the Apollo 11 1202/1201
"executive overflow" program alarms during powered descent) would be wired
into a real **Microsoft Fabric Real-Time Intelligence Operations Agent**
(see: https://learn.microsoft.com/en-us/fabric/real-time-intelligence/operations-agent-actions
and https://learn.microsoft.com/en-us/fabric/real-time-intelligence/anomaly-detection).

## 1. Data path

```
agc_simulator.py --> NDJSON (data/telemetry_stream.ndjson)
apollo-lander  --> WebSocket (simulator/web_app.py)
                  --> Fabric Eventstream (Custom App source, live inspection)
                  --> Eventhouse table AgcTelemetry (managed-identity streaming)
                  --> KQL anomaly detection (fabric/kql/anomaly_detection_queries.kql)
                  --> Operations Agent trigger
                  --> Teams notification + Copilot "Investigator insights"
                  --> Suggested remediation action
```

## 2. Monitored condition (the "goal")

The Operations Agent is configured to watch the `AgcTelemetry` Eventhouse
table for either the historical replay pattern or the playable training
scenario:

- **Signal:** `core_sets_used >= max_core_sets - 1` (executive job table
  nearly exhausted) **while** `radar_auto_slew == true`.
- **Confirmation:** an `event_type == "alarm"` row with `code in ("1202","1201")`
  follows within a few seconds.
- **Playable exercise signal:** seven `lander_program_memory` rows share a
  `memory_sample_id`. Derive total pool usage by summing
  `memory_program_used_words`, then compare each program's allocation over
  time. The program with the largest increase is the root-cause candidate.

This mirrors Fabric's real anomaly-detection workflow: a KQL-based
detection model (or `series_decompose_anomalies`) runs continuously against
the Eventhouse table, and the Operations Agent is the layer that turns a
detected anomaly into a notification + investigation + action.

## 3. Agent configuration (conceptual — Fabric portal steps)

1. In the Fabric workspace, open **Real-Time Intelligence** → the
   `Apollo11Eventhouse` Eventhouse → **Operations Agent**.
2. Create a new agent, "AGC Executive Overflow Watch", scoped to the
   `AgcTelemetry` table.
3. Set the playable trigger to query 14 in
   `anomaly_detection_queries.kql`, which derives pool utilization from
   the seven program rows.
4. Action 1 — **Notify**: post to a Teams channel ("Mission Control Ops")
   with the anomaly summary and a deep link to the KQL results.
5. Action 2 — **Investigate**: invoke Copilot **Investigator insights** to
   compare `memory_program_used_words` and
   `memory_program_growth_words_per_second` by program, verb, and noun.
   The allocation that diverges from the six stable programs identifies
   the workload to stop.
6. Action 3 — **Suggested remediation**: run a Fabric notebook (or Power
   Automate flow) that would, in the real mission, correspond to the
   controller action: when `V16 N68` is the only growing allocation,
   recommend the runbook command that stops that monitor. After the
   operator enters the command, verify that the `V16 N68` program state
   changes to `stopped` and its allocation drops to zero.

## 4. Why an agent (not just a static alert) helps here

A static threshold alert would only say "core sets exhausted." The
Operations Agent's value is in chaining: detection → correlation across
multiple signals (radar mode + job table + program) → natural-language
explanation → a proposed fix — which is exactly the reasoning Steve Bales'
backroom team (Jack Garman et al.) did manually in real time in 1969 using
a paper alarm cheat-sheet. This demo effectively re-implements that
human troubleshooting process as an automated Fabric agent pipeline.
