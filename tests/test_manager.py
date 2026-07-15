from anpr.dahua.manager import ONVIF_FLUSH_SECONDS, _flush_onvif, _merge_onvif


def test_merge_onvif_first_nonempty_wins():
    dest = {}
    # plate-only frame
    _merge_onvif(dest, {"plate": "ABC123", "country": "", "image_b64": None,
                        "vehicle_type": ""})
    assert dest["plate"] == "ABC123"
    # later frame brings the image and attributes
    _merge_onvif(dest, {"plate": "", "country": "DEU", "image_b64": "/9j/PIC",
                        "vehicle_type": "Sedan", "direction": "Approaching"})
    assert dest["plate"] == "ABC123"          # not overwritten
    assert dest["country"] == "DEU"
    assert dest["image_b64"] == "/9j/PIC"
    assert dest["vehicle_type"] == "Sedan"
    assert dest["direction"] == "Approaching"


def test_flush_onvif_emits_stale_plate_only_once():
    pending = {"1": {"data": {"plate": "XYZ"}, "last": 0.0, "emitted": False}}
    out = _flush_onvif(pending, ONVIF_FLUSH_SECONDS + 1)
    assert len(out) == 1 and out[0]["plate"] == "XYZ"
    assert pending["1"]["emitted"] is True
    # already emitted -> not emitted again
    assert _flush_onvif(pending, ONVIF_FLUSH_SECONDS + 2) == []


def test_flush_onvif_keeps_recent():
    pending = {"1": {"data": {"plate": "XYZ"}, "last": 100.0, "emitted": False}}
    assert _flush_onvif(pending, 101.0) == []   # seen recently, keep waiting
    assert "1" in pending
