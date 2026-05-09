"""aiohttp HTTP + WebSocket server for the live dashboard.

Two endpoints:
    GET  /          -> dashboard.html (single-page UI)
    WS   /ws        -> bidirectional knob updates + chord/stats events

The server holds a reference to the shared LiveKnobs (so it can write incoming
knob patches) and the LiveSession (so it can pull stats/last chord). The
LiveSession invokes `broadcast(...)` whenever a chord is emitted or stats tick.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict
from pathlib import Path

from aiohttp import WSMsgType, web

from bebop.live.comp_engine import LiveKnobs


_DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"


class DashboardServer:
    def __init__(
        self,
        knobs: LiveKnobs,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        device_label: str = "",
        output_path: str = "",
        rhythms: list[str],
        silence_rms: float = 0.005,
        midi_port: str = "",
    ) -> None:
        self.knobs = knobs
        self.host = host
        self.port = port
        self.device_label = device_label
        self.output_path = output_path
        self.rhythms = rhythms
        self.silence_rms = silence_rms
        self.midi_port = midi_port

        self._app = web.Application()
        self._app.router.add_get("/", self._index)
        self._app.router.add_get("/ws", self._websocket)
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._sockets: set[web.WebSocketResponse] = set()
        self._on_knob_change = None    # optional: async callable(LiveKnobs)
        self._on_panic = None           # optional: async callable()

    def on_knob_change(self, callback) -> None:
        self._on_knob_change = callback

    def on_panic(self, callback) -> None:
        self._on_panic = callback

    async def _index(self, request: web.Request) -> web.Response:
        return web.Response(body=_DASHBOARD_HTML.read_bytes(), content_type="text/html")

    async def _websocket(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=15)
        await ws.prepare(request)
        self._sockets.add(ws)
        try:
            # send initial state
            await ws.send_json({
                "type": "init",
                "device": self.device_label,
                "output_path": self.output_path,
                "rhythms": self.rhythms,
                "silence_rms": self.silence_rms,
                "midi_port": self.midi_port,
                "knobs": asdict(self.knobs),
            })
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    try:
                        payload = json.loads(msg.data)
                    except json.JSONDecodeError:
                        continue
                    if payload.get("type") == "knobs":
                        self._apply_knob_patch(payload)
                        # echo the new state to all clients so multi-tab stays in sync
                        await self._broadcast_no_throw({"type": "knobs", "knobs": asdict(self.knobs)})
                        if self._on_knob_change is not None:
                            try:
                                await self._on_knob_change(self.knobs)
                            except Exception as e:
                                print(f"[server] knob-change handler error: {e}")
                    elif payload.get("type") == "panic":
                        if self._on_panic is not None:
                            try:
                                await self._on_panic()
                            except Exception as e:
                                print(f"[server] panic handler error: {e}")
                elif msg.type == WSMsgType.ERROR:
                    print(f"[server] ws error: {ws.exception()}")
        finally:
            self._sockets.discard(ws)
        return ws

    def _apply_knob_patch(self, patch: dict) -> None:
        for field in ("spice", "voicing", "rhythm", "bpm", "seed", "enabled", "piano_bass"):
            if field in patch:
                setattr(self.knobs, field, patch[field])

    async def broadcast(self, message: dict) -> None:
        await self._broadcast_no_throw(message)

    async def _broadcast_no_throw(self, message: dict) -> None:
        if not self._sockets:
            return
        data = json.dumps(message)
        dead: list[web.WebSocketResponse] = []
        for ws in self._sockets:
            try:
                await ws.send_str(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._sockets.discard(ws)

    async def start(self) -> str:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, self.host, self.port)
        await self._site.start()
        return f"http://{self.host}:{self.port}/"

    async def stop(self) -> None:
        for ws in list(self._sockets):
            await ws.close()
        self._sockets.clear()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
            self._site = None
