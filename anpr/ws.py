"""WebSocket hub - pushes live events and camera status to browsers."""

import json
from typing import Any, Dict, Set

from fastapi import WebSocket


class WebSocketHub:
    def __init__(self) -> None:
        self._clients: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket) -> None:
        await ws.accept()
        self._clients.add(ws)

    def disconnect(self, ws: WebSocket) -> None:
        self._clients.discard(ws)

    async def broadcast(self, message: Dict[str, Any]) -> None:
        if not self._clients:
            return
        payload = json.dumps(message, default=str)
        dead = []
        for ws in list(self._clients):
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)
