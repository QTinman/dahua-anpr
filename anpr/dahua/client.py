"""HTTP client for a single Dahua camera.

Dahua cameras expose a CGI API over HTTP with Digest authentication.
The two endpoints used here:

* ``eventManager.cgi?action=attach&codes=[...]`` - long-lived multipart
  stream of events (this is how ANPR results are delivered).
* ``snapshot.cgi?channel=N`` - a jpeg snapshot, used as fallback when an
  event carries no embedded picture.
"""

import asyncio
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .parser import (
    MultipartEventParser,
    ParsedPart,
    parse_find_records,
    parse_finder_object,
)

# Record set names used by Dahua ITC / ANPR firmwares for the stored plate log.
# Tried in order; the first that yields records wins.
TRAFFIC_RECORD_NAMES = ("TrafficSnapEventInfo", "TrafficSnap", "TrafficRedList")

DEFAULT_EVENT_CODES = "TrafficJunction"
HEARTBEAT_SECONDS = 5
# Read timeout must comfortably exceed the heartbeat interval, otherwise a
# quiet-but-healthy connection would be treated as dead.
READ_TIMEOUT = HEARTBEAT_SECONDS * 6


class DahuaError(Exception):
    pass


class DahuaClient:
    def __init__(
        self,
        host: str,
        port: int = 80,
        username: str = "admin",
        password: str = "",
        use_https: bool = False,
    ):
        scheme = "https" if use_https else "http"
        self.base_url = f"{scheme}://{host}:{port}"
        self._auth = httpx.DigestAuth(username, password)
        # Cameras almost always have self-signed certificates.
        self._verify = False if use_https else True

    def _client(self, read_timeout: float) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            auth=self._auth,
            verify=self._verify,
            timeout=httpx.Timeout(connect=10.0, read=read_timeout,
                                  write=10.0, pool=10.0),
            trust_env=False,
        )

    async def get_device_info(self) -> Dict[str, str]:
        """Fetch device type / serial - used by 'Test connection'."""
        info: Dict[str, str] = {}
        async with self._client(read_timeout=10.0) as client:
            for action, key in (
                ("getDeviceType", "type"),
                ("getSerialNo", "serial"),
                ("getSoftwareVersion", "version"),
            ):
                url = f"{self.base_url}/cgi-bin/magicBox.cgi?action={action}"
                try:
                    resp = await client.get(url)
                except httpx.HTTPError as exc:
                    raise DahuaError(f"Connection failed: {exc}") from exc
                if resp.status_code == 401:
                    raise DahuaError("Authentication failed (check username/password)")
                if resp.status_code != 200:
                    raise DahuaError(f"Camera returned HTTP {resp.status_code}")
                # Response format: "type=IPC-XXXX\r\n" or "sn=..." etc.
                for line in resp.text.splitlines():
                    if "=" in line:
                        _, _, value = line.partition("=")
                        info[key] = value.strip()
                        break
        return info

    async def snapshot(self, channel: int = 1) -> Optional[bytes]:
        url = f"{self.base_url}/cgi-bin/snapshot.cgi?channel={channel}"
        try:
            async with self._client(read_timeout=15.0) as client:
                resp = await client.get(url)
                if resp.status_code == 200 and resp.content[:2] == b"\xff\xd8":
                    return resp.content
        except httpx.HTTPError:
            pass
        return None

    async def find_traffic_records(
        self, max_records: int = 500
    ) -> List[Dict[str, str]]:
        """Fetch stored ANPR records from the camera via RecordFinder.

        Returns a list of flat record dicts (newest-first as the camera
        provides them). Best-effort: if the firmware does not expose the
        record set, returns an empty list rather than raising.
        """
        base = f"{self.base_url}/cgi-bin/recordFinder.cgi"
        async with self._client(read_timeout=30.0) as client:
            for name in TRAFFIC_RECORD_NAMES:
                try:
                    records = await self._find_one(client, base, name, max_records)
                except httpx.HTTPError as exc:
                    raise DahuaError(f"History query failed: {exc}") from exc
                if records:
                    return records
        return []

    async def _find_one(
        self, client: httpx.AsyncClient, base: str, name: str, max_records: int
    ) -> List[Dict[str, str]]:
        create = await client.get(f"{base}?action=factory.create&name={name}")
        if create.status_code == 401:
            raise DahuaError("Authentication failed (check username/password)")
        if create.status_code != 200:
            return []
        obj = parse_finder_object(create.text)
        if obj is None:
            return []
        records: List[Dict[str, str]] = []
        try:
            await client.get(
                f"{base}?action=startFind&object={obj}&count={max_records}"
            )
            while len(records) < max_records:
                batch_size = min(100, max_records - len(records))
                resp = await client.get(
                    f"{base}?action=doFind&object={obj}&count={batch_size}"
                )
                if resp.status_code != 200:
                    break
                batch = parse_find_records(resp.text)
                if not batch:
                    break
                records.extend(batch)
                if len(batch) < batch_size:
                    break
        finally:
            # Always release the finder object on the camera.
            try:
                await client.get(f"{base}?action=stopFind&object={obj}")
                await client.get(f"{base}?action=factory.destroy&object={obj}")
            except httpx.HTTPError:
                pass
        return records[:max_records]

    async def stream_events(
        self, codes: str = DEFAULT_EVENT_CODES
    ) -> AsyncIterator[ParsedPart]:
        """Attach to the camera event stream and yield parsed parts.

        Runs until the connection drops, then raises DahuaError so the
        caller can decide how to reconnect.
        """
        codes = codes.strip() or DEFAULT_EVENT_CODES
        url = (
            f"{self.base_url}/cgi-bin/eventManager.cgi?action=attach"
            f"&codes=[{codes}]&heartbeat={HEARTBEAT_SECONDS}"
        )
        try:
            async with self._client(read_timeout=READ_TIMEOUT) as client:
                async with client.stream("GET", url) as resp:
                    if resp.status_code == 401:
                        raise DahuaError("Authentication failed (check username/password)")
                    if resp.status_code != 200:
                        raise DahuaError(f"Event attach returned HTTP {resp.status_code}")
                    boundary = _boundary_from_content_type(
                        resp.headers.get("content-type", "")
                    )
                    parser = MultipartEventParser(boundary)
                    async for chunk in resp.aiter_bytes():
                        for part in parser.feed(chunk):
                            yield part
        except httpx.HTTPError as exc:
            raise DahuaError(f"Event stream error: {exc}") from exc
        raise DahuaError("Event stream closed by camera")


def _boundary_from_content_type(content_type: str) -> str:
    for token in content_type.split(";"):
        token = token.strip()
        if token.lower().startswith("boundary="):
            return token.split("=", 1)[1].strip().strip('"')
    return "myboundary"


async def probe(host: str, port: int, username: str, password: str,
                use_https: bool = False) -> Dict[str, str]:
    """One-shot connection test used by the API."""
    client = DahuaClient(host, port, username, password, use_https)
    return await asyncio.wait_for(client.get_device_info(), timeout=20.0)
