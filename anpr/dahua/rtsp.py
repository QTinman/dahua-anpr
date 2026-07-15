"""Minimal RTSP client for receiving ONVIF metadata over TCP.

Dahua ITC/ANPR cameras publish the plate/vehicle images and the richest ANPR
attributes on an ONVIF metadata track carried by RTSP. This module speaks just
enough RTSP to receive that track:

  DESCRIBE (Digest auth) -> parse SDP -> find the ``application`` metadata
  track -> SETUP it over TCP-interleaved transport -> PLAY -> read RTP packets
  and reassemble the ONVIF XML documents.

Only the metadata track is set up, so no video/H.264 depayloading is needed -
each RTP payload is a fragment of ONVIF XML, reassembled on the RTP marker bit.

Pure standard library (asyncio + hashlib); no third-party RTSP dependency.
"""

import asyncio
import hashlib
import logging
import re
from typing import AsyncIterator, Dict, List, Optional, Tuple

log = logging.getLogger("anpr.rtsp")

RTSP_PORT = 554
KEEPALIVE_SECONDS = 25
# Guard against a runaway/garbage length field claiming a huge interleaved
# frame (protects memory if the stream desyncs).
MAX_INTERLEAVED = 4 * 1024 * 1024


class RtspError(Exception):
    pass


def _md5(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest()


class _Digest:
    """Holds a parsed WWW-Authenticate challenge and builds responses."""

    def __init__(self) -> None:
        self.realm = ""
        self.nonce = ""
        self.qop = ""
        self.opaque = ""
        self.algorithm = "MD5"
        self.ready = False

    def parse_challenge(self, header: str) -> None:
        self.realm = _kv(header, "realm")
        self.nonce = _kv(header, "nonce")
        self.qop = _kv(header, "qop")
        self.opaque = _kv(header, "opaque")
        self.algorithm = _kv(header, "algorithm") or "MD5"
        self.ready = bool(self.nonce)

    def header(self, user: str, password: str, method: str, uri: str) -> str:
        ha1 = _md5(f"{user}:{self.realm}:{password}")
        ha2 = _md5(f"{method}:{uri}")
        parts = [
            f'username="{user}"', f'realm="{self.realm}"', f'nonce="{self.nonce}"',
            f'uri="{uri}"',
        ]
        if self.qop:
            # RTSP typically uses qop=auth with a fixed nonce-count.
            nc, cnonce = "00000001", "0a4f113b"
            response = _md5(f"{ha1}:{self.nonce}:{nc}:{cnonce}:auth:{ha2}")
            parts += [f'response="{response}"', "qop=auth", f"nc={nc}",
                      f'cnonce="{cnonce}"']
        else:
            parts.append(f'response="{_md5(f"{ha1}:{self.nonce}:{ha2}")}"')
        if self.opaque:
            parts.append(f'opaque="{self.opaque}"')
        return "Digest " + ", ".join(parts)


def _kv(header: str, key: str) -> str:
    m = re.search(rf'{key}\s*=\s*"([^"]*)"', header)
    if m:
        return m.group(1)
    m = re.search(rf'{key}\s*=\s*([^,\s]+)', header)
    return m.group(1) if m else ""


class RtspMetadataClient:
    def __init__(self, host: str, port: int = RTSP_PORT, username: str = "",
                 password: str = "", channel: int = 1, subtype: int = 0):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.channel = channel
        self.subtype = subtype
        self._cseq = 0
        self._digest = _Digest()
        self._session = ""
        self._base = ""
        self._metadata_channel = 0
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None

    def _url(self, subtype: int, proto: str = "") -> str:
        url = (f"rtsp://{self.host}:{self.port}/cam/realmonitor"
               f"?channel={self.channel}&subtype={subtype}")
        return url + (f"&proto={proto}" if proto else "")

    def _candidate_urls(self) -> List[str]:
        # Dahua only includes the ONVIF metadata track when the stream is
        # requested the ONVIF way (proto=Onvif); the plain URL carries only
        # video/audio. Prefer the sub stream (subtype=1) - it carries the same
        # metadata but with lower-bandwidth video, which we pull only to keep
        # the session alive.
        urls = [
            self._url(1, "Onvif"),
            self._url(0, "Onvif"),
            self._url(self.subtype, "Onvif"),
        ]
        seen, ordered = set(), []
        for u in urls:
            if u not in seen:
                seen.add(u)
                ordered.append(u)
        return ordered

    @property
    def base_url(self) -> str:
        return self._base or self._url(self.subtype)

    async def stream_metadata(self) -> AsyncIterator[str]:
        """Connect and yield complete ONVIF metadata XML documents."""
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10.0)
        except (OSError, asyncio.TimeoutError) as exc:
            raise RtspError(f"RTSP connect failed: {exc}") from exc
        try:
            # The ONVIF metadata track is only present on the proto=Onvif
            # stream variants; try the candidates until one advertises it.
            track_url = None
            video_url = None
            for url in self._candidate_urls():
                self._base = url
                try:
                    sdp = await self._describe()
                    track_url, _pt = self._find_metadata_track(sdp)
                    video_url = self._find_video_track(sdp)
                    break
                except RtspError:
                    track_url = None
            if track_url is None:
                raise RtspError(
                    "no ONVIF metadata track on the RTSP stream - enable "
                    "metadata (Smart Plan / RTSP) on the camera")

            # Many Dahua firmwares only push metadata when the video track is
            # also pulled in the same session; set up video first (its RTP
            # keeps the session alive), then the metadata track.
            if video_url:
                await self._setup_track(video_url, "0-1")
                self._metadata_channel = await self._setup_track(track_url, "2-3")
            else:
                self._metadata_channel = await self._setup_track(track_url, "0-1")
            if not self._session:
                raise RtspError("SETUP returned no session id")
            await self._play()
            log.info("ONVIF metadata streaming from %s (metadata on interleaved "
                     "channel %d, video=%s)", self._base, self._metadata_channel,
                     "yes" if video_url else "no")
            # Signal that the session is established even before the first
            # capture arrives, so the camera shows connected during quiet times.
            yield ""
            # A periodic RTSP keepalive resets the camera's session timeout;
            # RTP data flow alone does not, so without it the camera drops the
            # session after ~60-90s.
            keepalive = asyncio.create_task(self._keepalive_loop())
            try:
                async for xml in self._read_metadata():
                    yield xml
            finally:
                keepalive.cancel()
        finally:
            await self._close()

    async def describe_all(self) -> Dict[str, object]:
        """DESCRIBE the candidate streams and report their SDP + media tracks.

        Diagnostic only (no SETUP/PLAY): used to find where a camera publishes
        its ONVIF metadata track.
        """
        results: Dict[str, object] = {}
        candidates = self._candidate_urls() + [self._url(1)]
        seen = set()
        for i, url in enumerate(candidates):
            if url in seen:
                continue
            seen.add(url)
            entry: Dict[str, object] = {"url": url}
            # A fresh connection per URL: some cameras close the socket after an
            # error, which would poison later probes on a shared connection.
            self._cseq = 0
            self._session = ""
            self._base = url
            try:
                self._reader, self._writer = await asyncio.wait_for(
                    asyncio.open_connection(self.host, self.port), timeout=10.0)
            except (OSError, asyncio.TimeoutError) as exc:
                entry["error"] = f"connect failed: {exc}"
                results[f"stream{i}"] = entry
                continue
            try:
                sdp = await self._describe()
                entry["media"] = _sdp_media_summary(sdp)
                entry["has_metadata"] = any(
                    m["media"] == "application" for m in entry["media"])
            except RtspError as exc:
                entry["error"] = str(exc)
            finally:
                await self._close()
            results[f"stream{i}"] = entry
        return results

    async def sample_metadata(self, max_seconds: float = 25.0) -> Dict[str, object]:
        """Collect a few metadata XML documents (images redacted) for
        inspection - used to see the exact fields a camera publishes."""
        samples: List[str] = []

        async def collect() -> None:
            async for xml in self.stream_metadata():
                if not xml.strip():
                    continue  # the connected-signal, not a document
                samples.append(_redact_images(xml))
                # Stop once we have a real capture frame (plate + picture).
                if "PlateNumber>" in xml and "/9j/" in xml:
                    return
                if len(samples) >= 10:
                    return

        try:
            await asyncio.wait_for(collect(), timeout=max_seconds)
        except asyncio.TimeoutError:
            pass
        except RtspError as exc:
            return {"error": str(exc), "samples": samples}
        return {"count": len(samples), "samples": samples[-6:]}

    async def _close(self) -> None:
        if self._writer is not None:
            # Best-effort TEARDOWN so the camera frees this session's connection
            # slot promptly (Dahua caps concurrent connections), instead of
            # holding it until its own timeout across repeated reconnects.
            if self._session:
                try:
                    self._cseq += 1
                    lines = [f"TEARDOWN {self.base_url} RTSP/1.0",
                             f"CSeq: {self._cseq}", f"Session: {self._session}"]
                    if self._digest.ready and self.username:
                        lines.append("Authorization: " + self._digest.header(
                            self.username, self.password, "TEARDOWN", self.base_url))
                    self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
                    await asyncio.wait_for(self._writer.drain(), timeout=2.0)
                except Exception:
                    pass
            self._session = ""
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=5.0)
            except Exception:
                pass
            self._writer = None
            self._reader = None

    # ------------------------------------------------------------- requests

    async def _request(self, method: str, url: str,
                       extra: Optional[Dict[str, str]] = None) -> Tuple[int, Dict[str, str], bytes]:
        """Send one RTSP request, transparently retrying once for Digest auth."""
        status, headers, body = await self._send(method, url, extra)
        if status == 401:
            challenge = headers.get("www-authenticate", "")
            if "Digest" in challenge:
                self._digest.parse_challenge(challenge)
                status, headers, body = await self._send(method, url, extra)
        if status != 200:
            raise RtspError(f"{method} returned RTSP {status}")
        return status, headers, body

    async def _send(self, method: str, url: str,
                    extra: Optional[Dict[str, str]]) -> Tuple[int, Dict[str, str], bytes]:
        self._cseq += 1
        lines = [f"{method} {url} RTSP/1.0", f"CSeq: {self._cseq}"]
        if self._digest.ready and self.username:
            lines.append("Authorization: " + self._digest.header(
                self.username, self.password, method, url))
        if self._session:
            lines.append(f"Session: {self._session}")
        for key, value in (extra or {}).items():
            lines.append(f"{key}: {value}")
        request = ("\r\n".join(lines) + "\r\n\r\n").encode()
        self._writer.write(request)
        await self._writer.drain()
        return await self._read_response()

    async def _read_response(self) -> Tuple[int, Dict[str, str], bytes]:
        status_line = await self._readline()
        m = re.match(rb"RTSP/1\.0 (\d+)", status_line)
        status = int(m.group(1)) if m else 0
        headers: Dict[str, str] = {}
        while True:
            line = await self._readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, _, value = line.partition(b":")
                headers[key.decode().strip().lower()] = value.decode().strip()
        body = b""
        length = headers.get("content-length")
        if length and length.isdigit():
            body = await self._reader.readexactly(int(length))
        return status, headers, body

    async def _readline(self) -> bytes:
        return await asyncio.wait_for(self._reader.readline(), timeout=30.0)

    async def _describe(self) -> str:
        _, _, body = await self._request(
            "DESCRIBE", self.base_url, {"Accept": "application/sdp"})
        return body.decode("utf-8", "replace")

    async def _setup_track(self, track_url: str, interleaved: str) -> int:
        """SETUP a track and return the RTP interleaved channel the camera
        actually assigned (which may differ from what we requested)."""
        _, headers, _ = await self._request(
            "SETUP", track_url,
            {"Transport": f"RTP/AVP/TCP;unicast;interleaved={interleaved}"})
        session = headers.get("session", "")
        if session and not self._session:
            self._session = session.split(";")[0].strip()
        transport = headers.get("transport", "")
        m = re.search(r"interleaved=(\d+)", transport)
        return int(m.group(1)) if m else int(interleaved.split("-")[0])

    async def _play(self) -> None:
        await self._request("PLAY", self.base_url, {"Range": "npt=0.000-"})

    async def _keepalive_loop(self) -> None:
        """Send a periodic GET_PARAMETER to keep the RTSP session from timing
        out. The reply arrives interleaved with RTP and is consumed by the
        read loop's RTSP-response handling."""
        while True:
            await asyncio.sleep(KEEPALIVE_SECONDS)
            try:
                self._cseq += 1
                lines = [f"GET_PARAMETER {self.base_url} RTSP/1.0",
                         f"CSeq: {self._cseq}", f"Session: {self._session}"]
                if self._digest.ready and self.username:
                    lines.append("Authorization: " + self._digest.header(
                        self.username, self.password, "GET_PARAMETER",
                        self.base_url))
                self._writer.write(("\r\n".join(lines) + "\r\n\r\n").encode())
                await self._writer.drain()
            except Exception:
                return

    # ------------------------------------------------------------------ SDP

    @staticmethod
    def _media_blocks(sdp: str) -> List[Tuple[str, str]]:
        blocks: List[Tuple[str, str]] = []
        media_type = ""
        lines: List[str] = []
        for line in sdp.splitlines():
            if line.startswith("m="):
                if media_type:
                    blocks.append((media_type, "\n".join(lines)))
                media_type = line[2:].split()[0]
                lines = [line]
            else:
                lines.append(line)
        if media_type:
            blocks.append((media_type, "\n".join(lines)))
        return blocks

    def _find_metadata_track(self, sdp: str) -> Tuple[str, int]:
        """Locate the ONVIF metadata (application) media track in the SDP."""
        for media_type, block in self._media_blocks(sdp):
            if media_type == "application":
                return self._absolute_control(_sdp_control(block)), \
                    _sdp_payload_type(block)
        raise RtspError("no ONVIF metadata (application) track in SDP - "
                        "enable metadata/RTSP or check the stream URL")

    def _find_video_track(self, sdp: str) -> Optional[str]:
        """Locate the video media track (pulled to keep the session alive)."""
        for media_type, block in self._media_blocks(sdp):
            if media_type == "video":
                control = _sdp_control(block)
                if control:
                    return self._absolute_control(control)
        return None

    def _absolute_control(self, control: str) -> str:
        if not control or control == "*":
            return self.base_url
        if control.startswith("rtsp://"):
            return control
        # Relative control is appended as a PATH segment, even when the base
        # URL has a query string (the live555/ffmpeg convention Dahua expects):
        #   rtsp://.../cam/realmonitor?channel=1&subtype=0&proto=Onvif/trackID=4
        # Appending it as a query parameter instead makes Dahua reject SETUP
        # with "451 Parameter Not Understood".
        sep = "" if self.base_url.endswith("/") else "/"
        return f"{self.base_url}{sep}{control}"

    # ------------------------------------------------------- RTP metadata

    async def _read_metadata(self) -> AsyncIterator[str]:
        """Read interleaved RTP and yield reassembled ONVIF XML documents."""
        fragments: List[bytes] = []
        try:
            while True:
                marker = await self._reader.readexactly(1)
                if marker != b"$":
                    # An RTSP response (e.g. keepalive reply) slipped in; skip.
                    await self._skip_rtsp_response(marker)
                    continue
                header = await self._reader.readexactly(3)
                length = int.from_bytes(header[1:3], "big")
                if length == 0 or length > MAX_INTERLEAVED:
                    raise RtspError(f"bad interleaved frame length {length}")
                packet = await self._reader.readexactly(length)
                channel = header[0]
                if channel != self._metadata_channel:  # video / RTCP - discard
                    continue
                payload, end_of_unit = _rtp_payload(packet)
                if payload:
                    fragments.append(payload)
                if end_of_unit and fragments:
                    xml = b"".join(fragments).decode("utf-8", "replace")
                    fragments = []
                    if "<" in xml:
                        yield xml
        except (asyncio.IncompleteReadError, ConnectionError, OSError) as exc:
            # Normal disconnect (camera closed the session) - reconnect cleanly.
            raise RtspError("metadata stream closed by camera") from exc

    async def _skip_rtsp_response(self, first_byte: bytes) -> None:
        # Consume the rest of the status line and headers of an interleaved
        # RTSP response, plus any body.
        rest = await self._reader.readline()
        headers: Dict[str, str] = {}
        while True:
            line = await self._reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, _, value = line.partition(b":")
                headers[key.decode().strip().lower()] = value.decode().strip()
        length = headers.get("content-length")
        if length and length.isdigit():
            await self._reader.readexactly(int(length))


def _redact_images(xml: str) -> str:
    """Replace base64 image content with a length marker so the sample is
    readable (a single image is ~600 KB of base64)."""
    return re.sub(
        r"(<tt:Image>)[^<]*(</tt:Image>)",
        lambda m: f"{m.group(1)}[image {len(m.group(0))} b64 chars]{m.group(2)}",
        xml,
    )


def _sdp_media_summary(sdp: str) -> List[Dict[str, str]]:
    """Summarise each m= media section: type, rtpmap and control URL."""
    summary: List[Dict[str, str]] = []
    current: Optional[Dict[str, str]] = None
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m="):
            if current:
                summary.append(current)
            parts = line[2:].split()
            current = {"media": parts[0] if parts else "",
                       "format": " ".join(parts[1:]) if len(parts) > 1 else "",
                       "rtpmap": "", "control": ""}
        elif current is not None and line.startswith("a=rtpmap:"):
            current["rtpmap"] = line[len("a=rtpmap:"):]
        elif current is not None and line.startswith("a=control:"):
            current["control"] = line[len("a=control:"):]
    if current:
        summary.append(current)
    return summary


def _sdp_control(block: str) -> str:
    for line in block.splitlines():
        line = line.strip()
        if line.startswith("a=control:"):
            return line[len("a=control:"):].strip()
    return ""


def _sdp_payload_type(block: str) -> int:
    m = re.search(r"a=rtpmap:(\d+)", block)
    if m:
        return int(m.group(1))
    first = block.splitlines()[0].split()
    return int(first[3]) if len(first) > 3 and first[3].isdigit() else 0


def _rtp_payload(packet: bytes) -> Tuple[bytes, bool]:
    """Return (payload, marker_bit) from an RTP packet.

    The marker bit signals the last packet of a metadata access unit, i.e. the
    end of one ONVIF XML document.
    """
    if len(packet) < 12:
        return b"", False
    cc = packet[0] & 0x0F
    extension = (packet[0] >> 4) & 0x01
    marker = (packet[1] >> 7) & 0x01
    offset = 12 + cc * 4
    if extension and len(packet) >= offset + 4:
        ext_words = int.from_bytes(packet[offset + 2:offset + 4], "big")
        offset += 4 + ext_words * 4
    if offset > len(packet):
        return b"", bool(marker)
    return packet[offset:], bool(marker)
