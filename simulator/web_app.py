"""Azure-ready HTTP and WebSocket host for the Apollo 11 DSKY demo."""

import asyncio
import contextlib
import json
import os
from pathlib import Path

from aiohttp import web
from azure.eventhub import EventData
from azure.eventhub.aio import EventHubProducerClient

from agc_simulator import Simulator


ROOT = Path(__file__).resolve().parent.parent
DSKY_DIR = ROOT / "dsky-ui"


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
        self.producer = EventHubProducerClient.from_connection_string(
            conn_str=connection_string,
            eventhub_name=eventhub_name,
        )

    async def publish(self, payload: dict):
        await self.queue.put(payload)

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


async def create_app() -> web.Application:
    app = web.Application()
    connection_string = os.environ.get("FABRIC_EVENTHUB_CONNECTION_STR")
    eventhub_name = os.environ.get("FABRIC_EVENTHUB_NAME")
    publisher = (
        FabricEventstreamPublisher(connection_string, eventhub_name)
        if connection_string and eventhub_name
        else None
    )
    simulator = Simulator(
        speed=float(os.environ.get("SIMULATION_SPEED", "15")),
        telemetry_sink=publisher.publish if publisher else None,
    )
    app["simulator"] = simulator
    app["fabric_publisher"] = publisher

    async def index(_request):
        return web.FileResponse(DSKY_DIR / "index.html")

    async def health(_request):
        return web.json_response({
            "status": "ok",
            "clients": len(simulator.clients),
            "simulationSpeed": simulator.speed,
            "fabricPublishing": publisher is not None,
            "fabricQueueDepth": publisher.queue.qsize() if publisher else 0,
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

    async def start_simulator(application):
        if publisher is not None:
            application["publisher_task"] = asyncio.create_task(publisher.run())
        application["simulator_task"] = asyncio.create_task(simulator.run_forever())

    async def stop_simulator(application):
        tasks = [application["simulator_task"]]
        if publisher is not None:
            tasks.append(application["publisher_task"])
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app.router.add_get("/", index)
    app.router.add_get("/healthz", health)
    app.router.add_get("/ws", websocket)
    app.router.add_get("/style.css", stylesheet)
    app.router.add_get("/app.js", javascript)
    app.router.add_get("/README.md", readme)
    app.router.add_get("/docs/io-signal-diagram.md", signal_diagram)
    app.on_startup.append(start_simulator)
    app.on_cleanup.append(stop_simulator)
    return app


if __name__ == "__main__":
    port = int(os.environ.get("SERVER_PORT", os.environ.get("PORT", "8000")))
    web.run_app(create_app(), host="0.0.0.0", port=port)
