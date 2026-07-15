"""Pydantic models shared between the API layer and the camera manager."""

from typing import Optional

from pydantic import BaseModel, Field


class CameraBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=80, ge=1, le=65535)
    username: str = Field(default="admin", max_length=100)
    password: str = Field(default="", max_length=255)
    use_https: bool = False
    channel: int = Field(default=1, ge=1)
    # Comma separated Dahua event codes to subscribe to.
    event_codes: str = "TrafficJunction"
    # Fetch a snapshot from the camera when an event has no embedded image.
    snapshot_on_event: bool = True
    # Use the ONVIF metadata stream (RTSP) as the source of events + images.
    # This pairs each plate with its own capture image (and plate cutout) from
    # the same frame, instead of matching images to events across streams.
    use_onvif: bool = False
    rtsp_port: int = Field(default=554, ge=1, le=65535)
    enabled: bool = True


class CameraCreate(CameraBase):
    pass


class CameraUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    host: Optional[str] = Field(default=None, min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    username: Optional[str] = Field(default=None, max_length=100)
    # None = keep existing password, "" = clear it.
    password: Optional[str] = Field(default=None, max_length=255)
    use_https: Optional[bool] = None
    channel: Optional[int] = Field(default=None, ge=1)
    event_codes: Optional[str] = None
    snapshot_on_event: Optional[bool] = None
    use_onvif: Optional[bool] = None
    rtsp_port: Optional[int] = Field(default=None, ge=1, le=65535)
    enabled: Optional[bool] = None


class Camera(CameraBase):
    id: int


class CameraPublic(BaseModel):
    """Camera as exposed over the API - password is never returned."""

    id: int
    name: str
    host: str
    port: int
    username: str
    use_https: bool
    channel: int
    event_codes: str
    snapshot_on_event: bool
    use_onvif: bool
    rtsp_port: int
    enabled: bool
    status: str = "disabled"
    status_detail: str = ""

    @classmethod
    def from_camera(cls, cam: Camera, status: str = "disabled", detail: str = "") -> "CameraPublic":
        return cls(
            id=cam.id,
            name=cam.name,
            host=cam.host,
            port=cam.port,
            username=cam.username,
            use_https=cam.use_https,
            channel=cam.channel,
            event_codes=cam.event_codes,
            snapshot_on_event=cam.snapshot_on_event,
            use_onvif=cam.use_onvif,
            rtsp_port=cam.rtsp_port,
            enabled=cam.enabled,
            status=status,
            status_detail=detail,
        )


class TestConnectionRequest(BaseModel):
    host: str
    port: int = 80
    username: str = "admin"
    password: str = ""
    use_https: bool = False
    # When testing an existing camera without re-typing the password.
    camera_id: Optional[int] = None


class AnprEvent(BaseModel):
    """Normalised ANPR event, independent of the camera vendor format."""

    camera_id: int
    camera_name: str = ""
    event_code: str = "TrafficJunction"
    plate: str = ""
    plate_color: str = ""
    plate_type: str = ""
    country: str = ""
    vehicle_type: str = ""
    vehicle_color: str = ""
    vehicle_brand: str = ""
    vehicle_size: str = ""
    speed: Optional[float] = None
    direction: str = ""
    lane: Optional[int] = None
    event_time: str = ""      # timestamp reported by the camera, if any
    received_at: str = ""     # server timestamp (ISO 8601)
    image_b64: Optional[str] = None


class ReportSettings(BaseModel):
    enabled: bool = False
    time: str = Field(default="23:59", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    directory: str = "reports"


class WhitelistCreate(BaseModel):
    plate: str = Field(min_length=1, max_length=32)
    label: str = Field(default="", max_length=100)


class WhitelistEntry(BaseModel):
    id: int
    plate: str
    label: str = ""
    created_at: str = ""


class AccessSettings(BaseModel):
    """Whitelist-driven gate control and email alerting (all opt-in)."""

    enabled: bool = False

    # Fire the capturing camera's alarm output when a whitelisted plate is seen.
    gate_enabled: bool = False
    # CGI paths on the camera. Defaults pulse alarm output 1 (AlarmOut[0]).
    gate_open_path: str = "/cgi-bin/configManager.cgi?action=setConfig&AlarmOut[0].Mode=1"
    gate_close_path: str = "/cgi-bin/configManager.cgi?action=setConfig&AlarmOut[0].Mode=0"
    gate_pulse_seconds: float = Field(default=2.0, ge=0, le=60)

    # Email when a plate is NOT in the whitelist.
    email_enabled: bool = False
    smtp_host: str = ""
    smtp_port: int = Field(default=587, ge=1, le=65535)
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    email_from: str = ""
    email_to: str = ""            # comma-separated recipients
    email_attach_image: bool = True

    # Ignore repeat sightings of the same plate within this many seconds.
    debounce_seconds: int = Field(default=15, ge=0, le=3600)


class AccessSettingsPublic(AccessSettings):
    """Same as AccessSettings but the SMTP password is never sent to clients."""

    smtp_password_set: bool = False

    @classmethod
    def from_settings(cls, s: AccessSettings) -> "AccessSettingsPublic":
        data = s.model_dump()
        data["smtp_password_set"] = bool(data.get("smtp_password"))
        data["smtp_password"] = ""
        return cls(**data)
