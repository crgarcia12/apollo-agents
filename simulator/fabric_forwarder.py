"""
Fabric Eventstream forwarder
=============================

Streams AGC telemetry (NDJSON, produced by ../simulator/agc_simulator.py
into ../data/telemetry_stream.ndjson) into a real Microsoft Fabric
Eventstream "Custom Endpoint" source, which is Event Hub-protocol
compatible. From there it lands in the AgcTelemetry Eventhouse table
(see ../fabric/kql/create_table_and_mapping.kql) for the Operations Agent
and Copilot Investigator insights to analyze (see
../fabric/operations_agent_playbook.md).

This is the missing "wire it up for real" piece: nothing in this repo
talks to an actual Fabric workspace until you run this script with a real
connection string. Without that, only the local NDJSON file and the
simulated/illustrative fabric/*.md docs exist.

Setup (one-time, in the Fabric portal):
    1. Workspace -> Real-Time Intelligence -> new Eventstream.
    2. Add source -> "Custom App" (Event Hub-compatible custom endpoint).
    3. Copy the "Connection string-primary key" and the event hub name it
       gives you (looks like `es_<guid>`).
    4. Add a destination -> Eventhouse -> table `AgcTelemetry` (create it
       first via ../fabric/kql/create_table_and_mapping.kql), using the
       ingestion mapping `AgcTelemetryMapping` and JSON format.

Usage:
    pip install -r requirements.txt   # now also installs azure-eventhub
    setx FABRIC_EVENTHUB_CONNECTION_STR "Endpoint=sb://...;SharedAccessKeyName=...;SharedAccessKey=...;EntityPath=es_xxxx"
    setx FABRIC_EVENTHUB_NAME "es_xxxx"

    # Live-tail mode (run this alongside agc_simulator.py):
    python fabric_forwarder.py --follow

    # One-shot backfill of whatever is already in telemetry_stream.ndjson:
    python fabric_forwarder.py --replay
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
STREAM_PATH = DATA_DIR / "telemetry_stream.ndjson"
BATCH_SIZE = 50
BATCH_INTERVAL_SEC = 2.0


def get_producer():
    """Lazily import + construct the Event Hub producer client so this
    script still works (in --dry-run) without azure-eventhub installed."""
    from azure.eventhub import EventHubProducerClient

    conn_str = os.environ.get("FABRIC_EVENTHUB_CONNECTION_STR")
    eventhub_name = os.environ.get("FABRIC_EVENTHUB_NAME")
    if not conn_str or not eventhub_name:
        print(
            "ERROR: Set FABRIC_EVENTHUB_CONNECTION_STR and FABRIC_EVENTHUB_NAME "
            "environment variables first (see the docstring in this file for "
            "where to find them in the Fabric portal), or pass --dry-run to "
            "test without a real Fabric connection.",
            file=sys.stderr,
        )
        sys.exit(1)
    return EventHubProducerClient.from_connection_string(
        conn_str=conn_str, eventhub_name=eventhub_name
    )


def send_batch(producer, lines, dry_run):
    from azure.eventhub import EventData

    if dry_run:
        for line in lines:
            print(f"[dry-run] would send: {line[:120]}")
        return
    batch = producer.create_batch()
    for line in lines:
        try:
            batch.add(EventData(line))
        except ValueError:
            # batch full -- flush and start a new one
            producer.send_batch(batch)
            batch = producer.create_batch()
            batch.add(EventData(line))
    if len(batch) > 0:
        producer.send_batch(batch)


def replay(producer, dry_run):
    """Send every line currently in telemetry_stream.ndjson, once."""
    if not STREAM_PATH.exists():
        print(f"No file at {STREAM_PATH} yet -- run agc_simulator.py first.")
        return
    lines = [l.strip() for l in STREAM_PATH.read_text(encoding="utf-8").splitlines() if l.strip()]
    print(f"Replaying {len(lines)} telemetry lines to Fabric Eventstream...")
    for i in range(0, len(lines), BATCH_SIZE):
        send_batch(producer, lines[i : i + BATCH_SIZE], dry_run)
    print("Done.")


def follow(producer, dry_run):
    """Tail telemetry_stream.ndjson like `tail -f` and forward new lines in
    small batches, so this can run continuously alongside agc_simulator.py."""
    print(f"Following {STREAM_PATH} -- Ctrl+C to stop.")
    while not STREAM_PATH.exists():
        time.sleep(0.5)
    with STREAM_PATH.open("r", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        pending = []
        last_flush = time.time()
        while True:
            line = f.readline()
            if line:
                pending.append(line.strip())
            if pending and (len(pending) >= BATCH_SIZE or time.time() - last_flush > BATCH_INTERVAL_SEC):
                send_batch(producer, pending, dry_run)
                print(f"[forwarder] sent {len(pending)} events")
                pending = []
                last_flush = time.time()
            if not line:
                time.sleep(0.2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--follow", action="store_true", help="Continuously tail and forward new telemetry")
    parser.add_argument("--replay", action="store_true", help="Send everything currently in the NDJSON file once, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be sent instead of requiring a real Fabric connection")
    args = parser.parse_args()

    if not args.follow and not args.replay:
        parser.error("Pass --follow or --replay")

    producer = None if args.dry_run else get_producer()
    try:
        if args.replay:
            replay(producer, args.dry_run)
        if args.follow:
            follow(producer, args.dry_run)
    finally:
        if producer is not None:
            producer.close()


if __name__ == "__main__":
    main()
