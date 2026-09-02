# Simulated Fabric Copilot Investigation — 1202/1201 Program Alarms

This is a representative transcript of what **Copilot Investigator insights**
in Microsoft Fabric Real-Time Intelligence would produce once the Operations
Agent (see `operations_agent_playbook.md`) fires on the `AgcTelemetry`
Eventhouse table populated by this demo's simulator. It is written to match
the real historical root cause, using the fields actually present in the
ingested data.

---

**[Teams — #mission-control-ops]**

**🔔 Operations Agent — AGC Executive Overflow Watch**
Anomaly detected at `GET 102:38:20` (± telemetry sample resolution).
`core_sets_used` reached 7/7 while `radar_auto_slew = true`. Alarm `1202`
recorded at `GET 102:38:23`, program `P63`.

**Copilot Investigator insights:**

> **Summary:** The executive job scheduler ran out of core sets
> (6→7 of 7 in use) immediately before program alarm **1202** fired.
> In the 30 seconds leading up to the alarm, `radar_auto_slew` was `true`
> for 100% of samples — this is the dominant correlated signal.
>
> **Root cause:** The rendezvous radar was left in AUTO/SLEW mode. Its
> Coupling Data Unit continued sending position data to the AGC even
> though the landing programs (P63/P64) did not need it. Because the
> radar's 800 Hz reference and the AGC's counter-timing were not
> synchronized in this configuration, the CDU generated interrupt-driven
> job requests far faster than intended, consuming executive core sets
> meant for guidance and navigation jobs.
>
> **Evidence from ingested telemetry:**
> - `AgcTelemetry` rows show `core_sets_used` climbing from a baseline of
>   ~2–3 to 8 within the seconds before each `alarm` row.
> - All three alarm events (`1202` at 102:38:23, `1202` at 102:42:18,
>   `1201` at 102:42:43) co-occur with `radar_auto_slew = true`.
> - No alarms occur in the sampled window once `radar_auto_slew` returns
>   to `false` (post-touchdown, per the `event` row at 102:45:57).
>
> **Was this mission-critical?** No — cross-referencing the alarm code
> against the known-safe alarm list (as Steve Bales' guidance officer
> console and Jack Garman's handwritten alarm chart did in real time),
> `1202`/`1201` indicate the executive discarded *low-priority* jobs and
> preserved the primary landing guidance jobs (`BAILOUT`/`POODOO`
> recovery path in `ALARM_AND_ABORT.agc`). Recommendation at the time,
> and reproduced here: **GO for continued landing.**
>
> **Suggested remediation (for a live operational system):** Take the
> rendezvous radar out of AUTO/SLEW during the landing phase so the CDU
> stops generating unnecessary interrupts, eliminating the root cause
> rather than relying on the executive's overflow-recovery path on every
> descent.

---

## How this was produced from the data model

| Investigator claim | Backing KQL / field |
|---|---|
| "core_sets_used climbing to 8" | `AgcTelemetry` `event_type == "telemetry_sample"`, query 2 in `anomaly_detection_queries.kql` |
| "radar_auto_slew true before each alarm" | query 5 (root-cause correlation join) |
| "alarms are non-fatal / GO" | `note` field on each `alarm` row, matches historical ALSJ transcript wording |
| "recommend disabling AUTO/SLEW" | derived recommendation layered on top of the correlation, mirrors the real post-mission review's advice against always leaving the radar in AUTO during PDI on later missions |

In a live Fabric tenant this transcript would instead be generated
on-demand by the Copilot Investigator insights feature reading directly
from the `AgcTelemetry` Eventhouse table — it is reproduced here statically
because this repository does not include live Fabric credentials.
