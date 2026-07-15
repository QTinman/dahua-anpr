import struct

from anpr.dahua.rtsp import RtspMetadataClient, _Digest, _rtp_payload


def test_digest_response_shape():
    d = _Digest()
    d.parse_challenge('Digest realm="R", nonce="N"')
    assert d.ready
    header = d.header("admin", "pw", "DESCRIBE", "rtsp://h/s")
    assert header.startswith("Digest ")
    assert 'username="admin"' in header
    assert 'response="' in header


def test_rtp_payload_marker_and_offset():
    # V=2, no CSRC, no extension, marker set, PT=107
    hdr = struct.pack("!BBHII", 0x80, 0x80 | 107, 1, 100, 0xABCD)
    payload, marker = _rtp_payload(hdr + b"<xml/>")
    assert payload == b"<xml/>"
    assert marker is True

    # marker clear
    hdr2 = struct.pack("!BBHII", 0x80, 107, 2, 100, 0xABCD)
    payload2, marker2 = _rtp_payload(hdr2 + b"frag")
    assert payload2 == b"frag"
    assert marker2 is False


def test_rtp_payload_with_csrc_and_extension():
    cc = 2
    b0 = 0x80 | (1 << 4) | cc  # extension bit + 2 CSRC
    hdr = struct.pack("!BBHII", b0, 107, 1, 0, 0)
    hdr += b"\x00" * (cc * 4)            # CSRC list
    hdr += struct.pack("!HH", 0, 1)      # ext header: profile + length(1 word)
    hdr += b"\x00" * 4                    # 1 extension word
    payload, _ = _rtp_payload(hdr + b"DATA")
    assert payload == b"DATA"


def test_find_metadata_track_selects_application():
    sdp = (
        "v=0\r\n"
        "m=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\na=control:trackID=0\r\n"
        "m=application 0 RTP/AVP 107\r\n"
        "a=rtpmap:107 vnd.onvif.metadata/90000\r\na=control:trackID=2\r\n"
    )
    c = RtspMetadataClient("10.0.0.5", 554, "admin", "pw", channel=1, subtype=0)
    url, pt = c._find_metadata_track(sdp)
    assert url.endswith("trackID=2")
    assert "channel=1" in url
    assert pt == 107


def test_find_metadata_track_absent_raises():
    sdp = "v=0\r\nm=video 0 RTP/AVP 96\r\na=control:trackID=0\r\n"
    c = RtspMetadataClient("10.0.0.5")
    try:
        c._find_metadata_track(sdp)
        assert False, "expected RtspError"
    except Exception as exc:
        assert "metadata" in str(exc).lower()
