"""Azure-ready HTTP and WebSocket host for the Apollo 11 DSKY demo."""

import asyncio
import contextlib
import io
import json
import math
import os
import re
import secrets
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from aiohttp import web
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubProducerClient
from azure.kusto.data import KustoConnectionStringBuilder
from azure.kusto.data.data_format import DataFormat, IngestionMappingKind
from azure.kusto.ingest import IngestionProperties, KustoStreamingIngestClient

from agc_simulator import Simulator


ROOT = Path(__file__).resolve().parent.parent
DSKY_DIR = ROOT / "dsky-ui"
LANDER_DIR = ROOT / "lander-2d"
AUDIO_DIR = ROOT / "audio"
DEFAULT_TELEMETRY_PATH = Path(
    os.environ.get("TELEMETRY_STREAM_PATH", ROOT / "data" / "telemetry_stream.ndjson")
)
LANDER_STREAM_PATH = Path(
    os.environ.get(
        "LANDER_TELEMETRY_STREAM_PATH",
        DEFAULT_TELEMETRY_PATH.with_name("lander_game_stream.ndjson"),
    )
)
LANDER_STREAM_MAX_BYTES = int(
    os.environ.get("LANDER_TELEMETRY_MAX_BYTES", str(25 * 1024 * 1024))
)

LANDER_MESSAGE_KINDS = {"lander_game_telemetry", "lander_game_event"}
LANDER_GAME_STATES = {"ready", "flying", "incident", "landed", "crashed"}
LANDER_SCENARIOS = {"standard_lander", "fabric_intervention"}
LANDER_INCIDENT_STATES = {"none", "waiting", "resolved"}
LANDER_DSKY_COMMAND_STATES = {
    "not_required",
    "armed",
    "entering",
    "rejected",
    "accepted",
}
LANDER_OPERATIONS_AGENT_STATES = {
    "not_enabled",
    "monitoring",
    "investigating",
    "recommendation_ready",
    "remediation_applied",
}
LANDER_MEMORY_PROGRAM_COUNT = 7
LANDER_MEMORY_PROGRAM_STATES = {
    "stable",
    "growing",
    "overflow",
    "stopped",
}
LANDER_REMEDIATION_COMMAND = "V21N68P0"
LANDER_TOKEN_TTL_SECONDS = 60
LANDER_MAX_CONNECTIONS = 64
LANDER_MAX_CONNECTIONS_PER_CLIENT = 3
LANDER_RATE_LIMIT_PER_SECOND = 12
LANDER_MAX_RATE_VIOLATIONS = 4
LANDER_SEEN_MESSAGE_TTL_SECONDS = 60 * 60
LANDER_MAX_SEEN_MESSAGE_IDS = 10_000
LANDER_NUMBER_LIMITS = {
    "sim_get_seconds": (0.0, 1_000_000.0),
    "game_elapsed_s": (0.0, 3_600.0),
    "incident_wait_seconds": (0.0, 3_600.0),
    "lander_x_m": (-20_000.0, 20_000.0),
    "lander_altitude_m": (-100.0, 20_000.0),
    "lander_vertical_speed_mps": (-2_000.0, 2_000.0),
    "lander_horizontal_speed_mps": (-2_000.0, 2_000.0),
    "lander_rotation_deg": (-360.0, 360.0),
    "lander_throttle_pct": (0.0, 100.0),
    "lander_fuel_pct": (0.0, 100.0),
    "landing_target_distance_m": (-20_000.0, 20_000.0),
    "touchdown_vertical_speed_mps": (0.0, 2_000.0),
    "touchdown_horizontal_speed_mps": (0.0, 2_000.0),
    "touchdown_angle_deg": (0.0, 360.0),
    "agc_memory_utilization_pct": (0.0, 100.0),
    "agc_memory_growth_words_per_second": (-10_000.0, 10_000.0),
    "operations_agent_confidence_pct": (0.0, 100.0),
}
LANDER_INTEGER_LIMITS = {
    "verb": (0, 99),
    "noun": (0, 99),
    "core_sets_used": (0, 100),
    "max_core_sets": (1, 100),
    "sequence": (0, 10_000_000),
    "agc_memory_used_words": (0, 4_096),
    "agc_memory_capacity_words": (1, 4_096),
    "memory_pool_capacity_words": (1, 4_096),
}
LANDER_BOOLEAN_FIELDS = {
    "control_left",
    "control_right",
    "control_thrust",
    "prog_alarm",
    "restart_lamp",
    "radar_auto_slew",
    "requires_fabric_action",
    "radio_altimeter_monitor_enabled",
    "memory_overflow",
}
LANDER_STRING_LIMITS = {
    "client_event_time": 64,
    "mission_get": 16,
    "event_type": 64,
    "game_state": 16,
    "scenario": 32,
    "incident_state": 16,
    "recommended_dsky_command": 32,
    "entered_dsky_command": 32,
    "dsky_command_status": 16,
    "program": 16,
    "code": 16,
    "active_alarm_code": 16,
    "note": 500,
    "offending_program": 32,
    "offending_task": 64,
    "operations_agent_state": 32,
    "operations_agent_action": 300,
}
MISSION_GET_PATTERN = re.compile(r"^\d{3}:\d{2}:\d{2}$")
EVENT_TYPE_PATTERN = re.compile(r"^[a-z0-9_]{1,64}$")
MEMORY_PROGRAM_ID_PATTERN = re.compile(r"^[A-Z0-9_]{1,32}$")


class AiohttpWebSocketAdapter:
    def __init__(self, socket: web.WebSocketResponse):
        self.socket = socket

    def __aiter__(self):
        return self.socket.__aiter__()

    async def send(self, message: str):
        await self.socket.send_str(message)


class FabricEventstreamPublisher:
    def __init__(self, connection_string: str, eventhub_name: str):
        self.queue = asyncio.Queue(maxsize=1000)
        self.dropped_messages = 0
        self.producer = EventHubProducerClient.from_connection_string(
            conn_str=connection_string,
            eventhub_name=eventhub_name,
        )

    def enqueue(self, payload: dict):
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped_messages += 1
            if self.dropped_messages == 1 or self.dropped_messages % 100 == 0:
                print(
                    "[fabric] Eventstream queue full; "
                    f"dropped {self.dropped_messages} telemetry sample(s)"
                )

    async def run(self):
        async with self.producer:
            while True:
                payloads = [await self.queue.get()]
                while len(payloads) < 50 and not self.queue.empty():
                    payloads.append(self.queue.get_nowait())

                events = [EventData(json.dumps(payload)) for payload in payloads]
                while True:
                    try:
                        await self.producer.send_batch(events)
                        break
                    except Exception as error:
                        print(f"[fabric] publish failed; retrying in 5s: {error}")
                        await asyncio.sleep(5)

                for _ in payloads:
                    self.queue.task_done()
                print(f"[fabric] published {len(payloads)} telemetry sample(s)")


class FabricKustoPublisher:
    def __init__(
        self,
        cluster_uri: str,
        database: str,
        table: str,
        mapping: str,
        managed_identity_client_id: str | None,
    ):
        self.queue = asyncio.Queue(maxsize=1000)
        self.dropped_messages = 0
        kcsb = KustoConnectionStringBuilder.with_aad_managed_service_identity_authentication(
            cluster_uri,
            client_id=managed_identity_client_id,
        )
        self.client = KustoStreamingIngestClient(kcsb)
        self.ingestion_properties = IngestionProperties(
            database=database,
            table=table,
            data_format=DataFormat.MULTIJSON,
            ingestion_mapping_reference=mapping,
            ingestion_mapping_kind=IngestionMappingKind.JSON,
        )

    def enqueue(self, payload: dict):
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped_messages += 1
            if self.dropped_messages == 1 or self.dropped_messages % 100 == 0:
                print(
                    "[fabric-kusto] Eventhouse queue full; "
                    f"dropped {self.dropped_messages} telemetry sample(s)"
                )

    def _ingest_batch(self, payloads: list[dict]):
        data = "\n".join(
            json.dumps(payload, separators=(",", ":"))
            for payload in payloads
        ).encode("utf-8")
        with io.BytesIO(data) as stream:
            self.client.ingest_from_stream(
                stream,
                ingestion_properties=self.ingestion_properties,
            )

    async def run(self):
        while True:
            payloads = [await self.queue.get()]
            await asyncio.sleep(0.25)
            while len(payloads) < 50 and not self.queue.empty():
                payloads.append(self.queue.get_nowait())

            while True:
                try:
                    await asyncio.to_thread(self._ingest_batch, payloads)
                    break
                except Exception as error:
                    print(f"[fabric-kusto] ingest failed; retrying in 5s: {error}")
                    await asyncio.sleep(5)

            for _ in payloads:
                self.queue.task_done()
            print(f"[fabric-kusto] ingested {len(payloads)} telemetry sample(s)")

    def close(self):
        self.client.close()


class FabricPublisherFanout:
    def __init__(self, publishers):
        self.publishers = publishers

    async def publish(self, payload: dict):
        for publisher in self.publishers:
            publisher.enqueue(payload)

    def queue_depth(self):
        return sum(publisher.queue.qsize() for publisher in self.publishers)

    def dropped_messages(self):
        return sum(publisher.dropped_messages for publisher in self.publishers)


class LanderTelemetrySink:
    def __init__(
        self,
        path: Path,
        publisher: FabricPublisherFanout | None,
        max_bytes: int,
    ):
        self.path = path
        self.publisher = publisher
        self.max_bytes = max_bytes
        self.stream = None
        self.lock = asyncio.Lock()
        self.bytes_written = 0

    async def start(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open("a", encoding="utf-8")
        self.bytes_written = self.path.stat().st_size if self.path.exists() else 0

    def _rotate(self):
        if self.stream is None:
            raise RuntimeError("Lander telemetry sink has not been started")

        self.stream.close()
        backup_path = Path(f"{self.path}.1")
        backup_path.unlink(missing_ok=True)
        if self.path.exists():
            self.path.replace(backup_path)
        self.stream = self.path.open("a", encoding="utf-8")
        self.bytes_written = 0

    async def publish(self, payload: dict):
        if self.stream is None:
            raise RuntimeError("Lander telemetry sink has not been started")

        serialized = json.dumps(payload, separators=(",", ":"))
        serialized_bytes = len(serialized.encode("utf-8")) + 1
        async with self.lock:
            if self.bytes_written + serialized_bytes > self.max_bytes:
                self._rotate()
            self.stream.write(serialized + "\n")
            self.stream.flush()
            self.bytes_written += serialized_bytes

        if self.publisher is not None:
            await self.publisher.publish(payload)

    async def close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


def _validated_number(payload: dict, name: str, lower: float, upper: float):
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    if not lower <= number <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return number


def _validated_integer(payload: dict, name: str, lower: int, upper: int):
    value = payload.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    if not lower <= value <= upper:
        raise ValueError(f"{name} must be between {lower} and {upper}")
    return value


def _validated_uuid(payload: dict, name: str, required: bool):
    value = payload.get(name)
    if value is None:
        if required:
            raise ValueError(f"{name} is required")
        return None
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    try:
        return str(uuid.UUID(value))
    except ValueError as error:
        raise ValueError(f"{name} must be a UUID") from error


def _client_key(request: web.Request) -> str:
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        return forwarded_for.split(",", maxsplit=1)[0].strip()
    return request.remote or "unknown"


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True
    parsed = urlparse(origin)
    forwarded_host = request.headers.get("X-Forwarded-Host")
    request_host = (
        forwarded_host.split(",", maxsplit=1)[0].strip()
        if forwarded_host
        else request.host
    )
    return parsed.netloc.casefold() == request_host.casefold()


def _validated_program_memory(payload: dict, scenario: str) -> list[dict]:
    value = payload.get("program_memory")
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("program_memory must be an array")
    if scenario != "fabric_intervention":
        if value:
            raise ValueError(
                "Standard lander telemetry cannot report program memory"
            )
        return []
    if len(value) != LANDER_MEMORY_PROGRAM_COUNT:
        raise ValueError(
            f"program_memory must contain {LANDER_MEMORY_PROGRAM_COUNT} programs"
        )

    programs = []
    program_ids = set()
    verb_noun_pairs = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each program_memory entry must be an object")

        program_id = entry.get("program_id")
        if (
            not isinstance(program_id, str)
            or not MEMORY_PROGRAM_ID_PATTERN.fullmatch(program_id)
        ):
            raise ValueError("program_memory program_id is invalid")
        if program_id in program_ids:
            raise ValueError("program_memory program_id values must be unique")
        program_ids.add(program_id)

        program_name = entry.get("program_name")
        if not isinstance(program_name, str) or not program_name:
            raise ValueError("program_memory program_name is required")

        verb = _validated_integer(entry, "verb", 0, 99)
        noun = _validated_integer(entry, "noun", 0, 99)
        used_words = _validated_integer(
            entry,
            "memory_used_words",
            0,
            4_096,
        )
        baseline_words = _validated_integer(
            entry,
            "memory_baseline_words",
            0,
            4_096,
        )
        growth = _validated_number(
            entry,
            "memory_growth_words_per_second",
            -10_000.0,
            10_000.0,
        )
        state = entry.get("state")
        if None in {verb, noun, used_words, baseline_words, growth}:
            raise ValueError("program_memory numeric fields are required")
        if state not in LANDER_MEMORY_PROGRAM_STATES:
            raise ValueError("program_memory state is invalid")

        verb_noun = (verb, noun)
        if verb_noun in verb_noun_pairs:
            raise ValueError("program_memory VERB/NOUN pairs must be unique")
        verb_noun_pairs.add(verb_noun)
        programs.append({
            "program_id": program_id,
            "program_name": program_name[:64],
            "verb": verb,
            "noun": noun,
            "memory_used_words": used_words,
            "memory_baseline_words": baseline_words,
            "memory_growth_words_per_second": growth,
            "state": state,
        })

    if (16, 68) not in verb_noun_pairs:
        raise ValueError("program_memory must include VERB 16 NOUN 68")
    return programs


def expand_program_memory_records(payload: dict) -> list[dict]:
    programs = payload.pop("program_memory", [])
    if not programs:
        return []

    sample_id = str(uuid.uuid4())
    capacity = payload.get("memory_pool_capacity_words")
    shared_fields = (
        "source",
        "schema_version",
        "player_id",
        "game_id",
        "session_id",
        "attempt_id",
        "connection_id",
        "event_time",
        "client_event_time",
        "mission_get",
        "sim_get_seconds",
        "game_elapsed_s",
        "scenario",
        "incident_id",
        "incident_state",
        "incident_wait_seconds",
        "requires_fabric_action",
        "game_state",
        "active_alarm_code",
        "sequence",
        "memory_overflow",
        "memory_pool_capacity_words",
        "operations_agent_detection_id",
    )
    shared = {
        name: payload[name]
        for name in shared_fields
        if name in payload
    }

    records = []
    for program in programs:
        used_words = program["memory_used_words"]
        utilization = (
            (used_words / capacity) * 100
            if capacity
            else None
        )
        record = {
            **shared,
            "kind": "lander_program_memory",
            "event_type": "program_memory_sample",
            "memory_sample_id": sample_id,
            "memory_program_id": program["program_id"],
            "memory_program_name": program["program_name"],
            "memory_program_verb": program["verb"],
            "memory_program_noun": program["noun"],
            "memory_program_used_words": used_words,
            "memory_program_baseline_words": program[
                "memory_baseline_words"
            ],
            "memory_program_growth_words_per_second": program[
                "memory_growth_words_per_second"
            ],
            "memory_program_state": program["state"],
        }
        if utilization is not None:
            record["memory_program_utilization_pct"] = utilization
        records.append(record)
    return records


def normalize_lander_message(payload: dict, connection_id: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Message must be a JSON object")

    kind = payload.get("kind")
    if kind not in LANDER_MESSAGE_KINDS:
        raise ValueError("Unsupported lander message kind")

    player_id = _validated_uuid(payload, "player_id", required=True)
    attempt_id = _validated_uuid(payload, "attempt_id", required=True)
    incident_id = _validated_uuid(payload, "incident_id", required=False)
    operations_agent_detection_id = _validated_uuid(
        payload,
        "operations_agent_detection_id",
        required=False,
    )
    message_id = _validated_uuid(
        payload,
        "message_id",
        required=kind == "lander_game_event",
    )
    normalized = {
        "kind": kind,
        "source": "apollo11_lander_game",
        "schema_version": "1.5",
        "player_id": player_id,
        "game_id": attempt_id,
        "session_id": attempt_id,
        "attempt_id": attempt_id,
        "connection_id": connection_id,
        "event_time": datetime.now(timezone.utc).isoformat(),
    }
    if message_id is not None:
        normalized["message_id"] = message_id
    if incident_id is not None:
        normalized["incident_id"] = incident_id
    if operations_agent_detection_id is not None:
        normalized["operations_agent_detection_id"] = (
            operations_agent_detection_id
        )

    for name, max_length in LANDER_STRING_LIMITS.items():
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{name} must be a string")
        normalized[name] = value[:max_length]

    event_type = normalized.get("event_type")
    if event_type is None or not EVENT_TYPE_PATTERN.fullmatch(event_type):
        raise ValueError("event_type must use lowercase letters, numbers, or underscores")

    game_state = normalized.get("game_state")
    if game_state not in LANDER_GAME_STATES:
        raise ValueError("game_state is invalid")

    mission_get = normalized.get("mission_get")
    if mission_get is not None and not MISSION_GET_PATTERN.fullmatch(mission_get):
        raise ValueError("mission_get must use HHH:MM:SS")
    if mission_get is not None:
        _, minutes, seconds = (int(part) for part in mission_get.split(":"))
        if minutes >= 60 or seconds >= 60:
            raise ValueError("mission_get minutes and seconds must be below 60")

    client_event_time = normalized.get("client_event_time")
    if client_event_time is not None:
        try:
            datetime.fromisoformat(client_event_time.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("client_event_time must be ISO-8601") from error

    for name, (lower, upper) in LANDER_NUMBER_LIMITS.items():
        value = _validated_number(payload, name, lower, upper)
        if value is not None:
            normalized[name] = value

    for name, (lower, upper) in LANDER_INTEGER_LIMITS.items():
        value = _validated_integer(payload, name, lower, upper)
        if value is not None:
            normalized[name] = value

    for name in LANDER_BOOLEAN_FIELDS:
        value = payload.get(name)
        if value is None:
            continue
        if not isinstance(value, bool):
            raise ValueError(f"{name} must be a boolean")
        normalized[name] = value

    scenario = normalized.setdefault("scenario", "standard_lander")
    if scenario not in LANDER_SCENARIOS:
        raise ValueError("scenario is invalid")
    programs = _validated_program_memory(payload, scenario)
    if programs:
        normalized["program_memory"] = programs

    incident_state = normalized.setdefault("incident_state", "none")
    if incident_state not in LANDER_INCIDENT_STATES:
        raise ValueError("incident_state is invalid")

    normalized.setdefault("incident_wait_seconds", 0.0)
    requires_fabric_action = normalized.setdefault(
        "requires_fabric_action",
        False,
    )
    if incident_state != "none" and scenario != "fabric_intervention":
        raise ValueError("Only the Fabric intervention scenario can report incidents")
    if incident_state != "none" and incident_id is None:
        raise ValueError("incident_id is required once an incident starts")
    if normalized.get("game_state") == "incident" and (
        incident_state != "waiting" or not requires_fabric_action
    ):
        raise ValueError("Paused incident telemetry must require Fabric action")
    if incident_state != "waiting" and requires_fabric_action:
        raise ValueError("requires_fabric_action is only valid while waiting")

    if scenario == "fabric_intervention" and "dsky_command_status" not in normalized:
        if incident_state == "resolved":
            normalized["dsky_command_status"] = "accepted"
            normalized["entered_dsky_command"] = LANDER_REMEDIATION_COMMAND
            normalized["radio_altimeter_monitor_enabled"] = False
        else:
            normalized["dsky_command_status"] = "armed"

    dsky_command_status = normalized.setdefault("dsky_command_status", "not_required")
    if dsky_command_status not in LANDER_DSKY_COMMAND_STATES:
        raise ValueError("dsky_command_status is invalid")

    radio_altimeter_monitor_enabled = normalized.setdefault(
        "radio_altimeter_monitor_enabled",
        True,
    )
    if scenario == "fabric_intervention":
        provided_recommendation = normalized.get("recommended_dsky_command", "")
        if provided_recommendation not in {"", LANDER_REMEDIATION_COMMAND}:
            raise ValueError("recommended_dsky_command is invalid")

        default_agent_state = "monitoring"
        if incident_state == "resolved":
            default_agent_state = "remediation_applied"
        elif (
            incident_state == "waiting"
            and provided_recommendation == LANDER_REMEDIATION_COMMAND
        ):
            default_agent_state = "recommendation_ready"
        operations_agent_state = normalized.setdefault(
            "operations_agent_state",
            default_agent_state,
        )
        if operations_agent_state not in LANDER_OPERATIONS_AGENT_STATES:
            raise ValueError("operations_agent_state is invalid")
        if operations_agent_state == "not_enabled":
            raise ValueError(
                "Fabric intervention telemetry must enable the Operations Agent"
            )

        recommendation_available = operations_agent_state in {
            "recommendation_ready",
            "remediation_applied",
        }
        normalized["recommended_dsky_command"] = (
            LANDER_REMEDIATION_COMMAND if recommendation_available else ""
        )
    else:
        normalized["recommended_dsky_command"] = ""
        operations_agent_state = normalized.setdefault(
            "operations_agent_state",
            "not_enabled",
        )
        if operations_agent_state != "not_enabled":
            raise ValueError(
                "Standard lander telemetry cannot report Operations Agent activity"
            )
        if dsky_command_status != "not_required":
            raise ValueError("Standard lander telemetry cannot report a DSKY command")

    normalized.setdefault("memory_overflow", False)
    if programs:
        capacity = normalized.get("memory_pool_capacity_words")
        if capacity is None:
            raise ValueError(
                "memory_pool_capacity_words is required with program_memory"
            )
        total_used_words = sum(
            program["memory_used_words"]
            for program in programs
        )
        if total_used_words > capacity:
            raise ValueError(
                "program_memory exceeds memory_pool_capacity_words"
            )
        if normalized["memory_overflow"] and total_used_words != capacity:
            raise ValueError(
                "memory_overflow requires program memory to equal capacity"
            )
    entered_dsky_command = normalized.setdefault("entered_dsky_command", "")
    if dsky_command_status == "accepted" and (
        entered_dsky_command != LANDER_REMEDIATION_COMMAND
        or radio_altimeter_monitor_enabled
    ):
        raise ValueError("Accepted DSKY command telemetry is inconsistent")
    if incident_state == "resolved" and dsky_command_status != "accepted":
        raise ValueError("A resolved incident requires an accepted DSKY command")

    return normalized


async def create_app() -> web.Application:
    app = web.Application()
    connection_string = os.environ.get("FABRIC_EVENTHUB_CONNECTION_STR")
    eventhub_name = os.environ.get("FABRIC_EVENTHUB_NAME")
    eventstream_publisher = (
        FabricEventstreamPublisher(connection_string, eventhub_name)
        if connection_string and eventhub_name
        else None
    )
    kusto_cluster_uri = os.environ.get("FABRIC_KUSTO_CLUSTER_URI")
    kusto_database = os.environ.get("FABRIC_KUSTO_DATABASE")
    kusto_publisher = (
        FabricKustoPublisher(
            cluster_uri=kusto_cluster_uri,
            database=kusto_database,
            table=os.environ.get("FABRIC_KUSTO_TABLE", "AgcTelemetry"),
            mapping=os.environ.get(
                "FABRIC_KUSTO_MAPPING",
                "AgcTelemetryMapping",
            ),
            managed_identity_client_id=os.environ.get("AZURE_CLIENT_ID"),
        )
        if kusto_cluster_uri and kusto_database
        else None
    )
    fabric_publishers = [
        publisher
        for publisher in (eventstream_publisher, kusto_publisher)
        if publisher is not None
    ]
    publisher = (
        FabricPublisherFanout(fabric_publishers)
        if fabric_publishers
        else None
    )
    simulator = Simulator(
        speed=float(os.environ.get("SIMULATION_SPEED", "15")),
        telemetry_sink=publisher.publish if publisher else None,
    )
    lander_sink = LanderTelemetrySink(
        LANDER_STREAM_PATH,
        publisher,
        LANDER_STREAM_MAX_BYTES,
    )
    app["simulator"] = simulator
    app["fabric_publisher"] = publisher
    app["eventstream_publisher"] = eventstream_publisher
    app["kusto_publisher"] = kusto_publisher
    app["lander_sink"] = lander_sink
    app["lander_clients"] = set()
    app["lander_client_counts"] = {}
    app["lander_tokens"] = {}
    app["lander_seen_message_ids"] = {}
    app["lander_messages_received"] = 0
    app["lander_messages_rejected"] = 0

    async def index(_request):
        return web.FileResponse(DSKY_DIR / "index.html")

    async def health(_request):
        return web.json_response({
            "status": "ok",
            "clients": len(simulator.clients),
            "simulationSpeed": simulator.speed,
            "fabricPublishing": publisher is not None,
            "eventstreamPublishing": eventstream_publisher is not None,
            "eventhousePublishing": kusto_publisher is not None,
            "fabricQueueDepth": publisher.queue_depth() if publisher else 0,
            "fabricDroppedMessages": publisher.dropped_messages() if publisher else 0,
            "eventstreamQueueDepth": (
                eventstream_publisher.queue.qsize()
                if eventstream_publisher
                else 0
            ),
            "eventhouseQueueDepth": (
                kusto_publisher.queue.qsize()
                if kusto_publisher
                else 0
            ),
            "landerClients": len(app["lander_clients"]),
            "landerMessagesReceived": app["lander_messages_received"],
            "landerMessagesRejected": app["lander_messages_rejected"],
        })

    async def websocket(request):
        socket = web.WebSocketResponse(heartbeat=30)
        await socket.prepare(request)
        await simulator.register(AiohttpWebSocketAdapter(socket))
        return socket

    async def stylesheet(_request):
        return web.FileResponse(DSKY_DIR / "style.css")

    async def javascript(_request):
        return web.FileResponse(DSKY_DIR / "app.js")

    async def readme(_request):
        return web.FileResponse(ROOT / "README.md")

    async def signal_diagram(_request):
        return web.FileResponse(ROOT / "docs" / "io-signal-diagram.md")

    async def lander_index(_request):
        return web.FileResponse(LANDER_DIR / "index.html")

    async def lander_stylesheet(_request):
        return web.FileResponse(LANDER_DIR / "style.css")

    async def lander_javascript(_request):
        return web.FileResponse(LANDER_DIR / "app.js")

    async def lander_descent_audio(_request):
        return web.FileResponse(AUDIO_DIR / "descent.mp3")

    async def lander_outcome_audio(_request):
        return web.FileResponse(AUDIO_DIR / "landed.mp3")

    async def lander_session(request):
        if not _same_origin(request):
            raise web.HTTPForbidden(text="Cross-origin lander sessions are not allowed")

        now = time.monotonic()
        expired_tokens = [
            token
            for token, details in app["lander_tokens"].items()
            if details["expires_at"] <= now
        ]
        for token in expired_tokens:
            del app["lander_tokens"][token]

        token = secrets.token_urlsafe(32)
        app["lander_tokens"][token] = {
            "expires_at": now + LANDER_TOKEN_TTL_SECONDS,
            "client_key": _client_key(request),
        }
        return web.json_response({
            "token": token,
            "expiresInSeconds": LANDER_TOKEN_TTL_SECONDS,
        })

    async def lander_websocket(request):
        if not _same_origin(request):
            raise web.HTTPForbidden(text="Cross-origin lander sockets are not allowed")

        token = request.query.get("token")
        token_details = app["lander_tokens"].pop(token, None) if token else None
        if (
            token_details is None
            or token_details["expires_at"] <= time.monotonic()
            or token_details["client_key"] != _client_key(request)
        ):
            raise web.HTTPUnauthorized(text="A valid one-time lander session token is required")

        client_key = _client_key(request)
        client_count = app["lander_client_counts"].get(client_key, 0)
        if len(app["lander_clients"]) >= LANDER_MAX_CONNECTIONS:
            raise web.HTTPServiceUnavailable(text="The lander simulator is at capacity")
        if client_count >= LANDER_MAX_CONNECTIONS_PER_CLIENT:
            raise web.HTTPTooManyRequests(text="Too many lander connections from this client")

        socket = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024)
        await socket.prepare(request)
        connection_id = str(uuid.uuid4())
        app["lander_clients"].add(socket)
        app["lander_client_counts"][client_key] = client_count + 1
        await socket.send_json({
            "kind": "lander_session",
            "connection_id": connection_id,
            "fabricPublishing": publisher is not None,
            "eventstreamPublishing": eventstream_publisher is not None,
            "eventhousePublishing": kusto_publisher is not None,
        })

        recent_messages = deque()
        rate_violations = 0
        try:
            async for message in socket:
                if message.type == web.WSMsgType.TEXT:
                    now = time.monotonic()
                    while recent_messages and recent_messages[0] <= now - 1:
                        recent_messages.popleft()
                    if len(recent_messages) >= LANDER_RATE_LIMIT_PER_SECOND:
                        rate_violations += 1
                        app["lander_messages_rejected"] += 1
                        await socket.send_json({
                            "kind": "lander_error",
                            "message": "Telemetry rate limit exceeded",
                        })
                        if rate_violations >= LANDER_MAX_RATE_VIOLATIONS:
                            await socket.close(
                                code=web.WSCloseCode.POLICY_VIOLATION,
                                message=b"Telemetry rate limit exceeded",
                            )
                            break
                        continue
                    recent_messages.append(now)

                    try:
                        incoming = json.loads(message.data)
                        payload = normalize_lander_message(incoming, connection_id)
                    except (json.JSONDecodeError, ValueError) as error:
                        app["lander_messages_rejected"] += 1
                        await socket.send_json({
                            "kind": "lander_error",
                            "message": str(error),
                        })
                        continue

                    message_id = payload.get("message_id")
                    if message_id in app["lander_seen_message_ids"]:
                        await socket.send_json({
                            "kind": "lander_ack",
                            "message_id": message_id,
                        })
                        continue

                    program_memory_records = expand_program_memory_records(
                        payload,
                    )
                    await lander_sink.publish(payload)
                    for program_memory_record in program_memory_records:
                        await lander_sink.publish(program_memory_record)
                    app["lander_messages_received"] += 1
                    if message_id is not None:
                        app["lander_seen_message_ids"][message_id] = time.monotonic()
                        await socket.send_json({
                            "kind": "lander_ack",
                            "message_id": message_id,
                        })

                    if len(app["lander_seen_message_ids"]) > LANDER_MAX_SEEN_MESSAGE_IDS:
                        cutoff = time.monotonic() - LANDER_SEEN_MESSAGE_TTL_SECONDS
                        retained_message_ids = {
                            seen_id: seen_at
                            for seen_id, seen_at in app["lander_seen_message_ids"].items()
                            if seen_at > cutoff
                        }
                        if len(retained_message_ids) > LANDER_MAX_SEEN_MESSAGE_IDS:
                            newest_ids = sorted(
                                retained_message_ids.items(),
                                key=lambda item: item[1],
                                reverse=True,
                            )[:LANDER_MAX_SEEN_MESSAGE_IDS]
                            retained_message_ids = dict(newest_ids)
                        app["lander_seen_message_ids"] = retained_message_ids
                elif message.type == web.WSMsgType.ERROR:
                    print(f"[lander] websocket error: {socket.exception()}")
                    break
                else:
                    await socket.send_json({
                        "kind": "lander_error",
                        "message": "Only JSON text messages are accepted",
                    })
        finally:
            app["lander_clients"].discard(socket)
            remaining = app["lander_client_counts"].get(client_key, 1) - 1
            if remaining > 0:
                app["lander_client_counts"][client_key] = remaining
            else:
                app["lander_client_counts"].pop(client_key, None)

        return socket

    async def mission_timeline(_request):
        return web.FileResponse(ROOT / "data" / "mission_timeline.json")

    async def operations_agent_playbook(_request):
        return web.FileResponse(ROOT / "fabric" / "operations_agent_playbook.md")

    async def start_simulator(application):
        await lander_sink.start()
        application["publisher_tasks"] = [
            asyncio.create_task(fabric_publisher.run())
            for fabric_publisher in fabric_publishers
        ]
        application["simulator_task"] = asyncio.create_task(simulator.run_forever())

    async def stop_simulator(application):
        tasks = [
            application["simulator_task"],
            *application["publisher_tasks"],
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if kusto_publisher is not None:
            kusto_publisher.close()
        await lander_sink.close()

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/style.css", stylesheet)
    app.router.add_get("/app.js", javascript)
    app.router.add_get("/README.md", readme)
    app.router.add_get("/docs/io-signal-diagram.md", signal_diagram)
    app.router.add_get("/lander", lander_index)
    app.router.add_get("/lander/", lander_index)
    app.router.add_get("/apollo-lander", lander_index)
    app.router.add_get("/apollo-lander/", lander_index)
    app.router.add_get("/lander/style.css", lander_stylesheet)
    app.router.add_get("/lander/app.js", lander_javascript)
    app.router.add_get("/audio/descent.mp3", lander_descent_audio)
    app.router.add_get("/audio/landed.mp3", lander_outcome_audio)
    app.router.add_get("/lander/session", lander_session)
    app.router.add_get("/lander/ws", lander_websocket)
    app.router.add_get("/data/mission_timeline.json", mission_timeline)
    app.router.add_get("/fabric/operations_agent_playbook.md", operations_agent_playbook)
    app.on_startup.append(start_simulator)
    app.on_cleanup.append(stop_simulator)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "8000")))
    web.run_app(create_app(), host="0.0.0.0", port=port)
