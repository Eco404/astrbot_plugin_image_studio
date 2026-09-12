from __future__ import annotations

import ctypes
import io
import os
from datetime import datetime, timezone

import pytest
from astrbot_plugin_image_studio import external_timestamps as times
from astrbot_plugin_image_studio.external_gallery import file_fingerprint
from PIL import Image, PngImagePlugin


def png_bytes(fields=None):
    metadata = PngImagePlugin.PngInfo()
    for key, value in (fields or {}).items():
        metadata.add_text(key, value)
    output = io.BytesIO()
    Image.new("RGB", (3, 5), "blue").save(output, "PNG", pnginfo=metadata)
    return output.getvalue()


def test_nai_timestamp_priority_and_custom_directory_ignores_nai_filename(
    tmp_path, monkeypatch
):
    data = png_bytes({"Creation Time": "2020-01-02T03:04:05Z"})
    path = tmp_path / "nai_1700000000000000000.png"
    path.write_bytes(data)
    monkeypatch.setattr(times, "filesystem_birthtime", lambda *args: 1800000000)
    fingerprint = file_fingerprint(path.stat())
    nai = times.external_image_times(
        filename=path.name, data=data, path=path, fingerprint=fingerprint, nai=True
    )
    assert nai["time_source"] == "nai_filename" and nai["created_at"] == 1700000000
    assert (
        nai["metadata_created_at"]
        == datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    )
    assert nai["file_birthtime"] == 1800000000
    directory = times.external_image_times(
        filename=path.name, data=data, path=path, fingerprint=fingerprint
    )
    assert directory["time_source"] == "metadata"
    assert directory["created_at"] == nai["metadata_created_at"]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"Creation Time": "invalid"},
        {"DateTime": "2020:01:02 03:04:05", "ModifyDate": "2020-01-02T03:04:05Z"},
    ],
)
def test_missing_invalid_and_modification_metadata_fall_back_to_birth_then_mtime(
    tmp_path, monkeypatch, fields
):
    data = png_bytes(fields)
    path = tmp_path / "ordinary.png"
    path.write_bytes(data)
    fingerprint = file_fingerprint(path.stat())
    monkeypatch.setattr(times, "filesystem_birthtime", lambda *args: 1600000000)
    result = times.external_image_times(
        filename=path.name, data=data, path=path, fingerprint=fingerprint
    )
    assert result["time_source"] == "btime" and result["created_at"] == 1600000000
    assert result["metadata_created_at"] is None
    monkeypatch.setattr(times, "filesystem_birthtime", lambda *args: None)
    result = times.external_image_times(
        filename=path.name, data=data, path=path, fingerprint=fingerprint
    )
    assert result["time_source"] == "mtime"
    assert result["created_at"] == fingerprint["mtime_ns"] / 1_000_000_000


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "PNG"])
def test_exif_original_time_offset_and_subseconds(fmt):
    exif = Image.Exif()
    exif[36867] = "2020:01:02 03:04:05"
    exif[36881] = "+08:00"
    exif[37521] = "1234"
    exif[306] = "2025:01:01 00:00:00"
    output = io.BytesIO()
    Image.new("RGB", (3, 5), "blue").save(output, fmt, exif=exif)
    expected = datetime.fromisoformat("2020-01-02T03:04:05.1234+08:00").timestamp()
    assert times.image_creation_timestamp(output.getvalue()) == pytest.approx(expected)


@pytest.mark.parametrize(
    "xml",
    [
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreateDate="2020-01-02T03:04:05Z" xmp:ModifyDate="2025-01-01T00:00:00Z" /></rdf:RDF></x:xmpmeta>',
        '<root xmlns:p="http://ns.adobe.com/photoshop/1.0/"><p:DateCreated>2020-01-02T03:04:05Z</p:DateCreated></root>',
    ],
)
def test_xmp_creation_attributes_and_elements(xml):
    expected = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc).timestamp()
    assert (
        times.image_creation_timestamp(png_bytes({"XML:com.adobe.xmp": xml}))
        == expected
    )


def test_xmp_entities_and_modified_only_packet_are_ignored():
    assert (
        times._xmp_creation_fields(
            '<!DOCTYPE root [<!ENTITY d "2020-01-02T03:04:05Z">]><root><CreateDate>&d;</CreateDate></root>'
        )
        == {}
    )
    assert (
        times.image_creation_timestamp(
            png_bytes(
                {
                    "XML:com.adobe.xmp": "<root><ModifyDate>2020-01-02T03:04:05Z</ModifyDate></root>"
                }
            )
        )
        is None
    )


def test_birthtime_never_substitutes_ctime_and_rejects_replaced_file(
    tmp_path, monkeypatch
):
    path = tmp_path / "one.png"
    path.write_bytes(png_bytes())
    fingerprint = file_fingerprint(path.stat())
    monkeypatch.setattr(times, "_STATX", None)
    if not hasattr(path.stat(), "st_birthtime"):
        assert times.filesystem_birthtime(path, fingerprint) is None
    path.unlink()
    path.write_bytes(b"replacement")
    assert times.filesystem_birthtime(path, fingerprint) is None


def test_statx_requires_birthtime_support_and_matches_file_identity(
    tmp_path, monkeypatch
):
    path = tmp_path / "one.png"
    path.write_bytes(png_bytes())
    fingerprint = file_fingerprint(path.stat())
    assert ctypes.sizeof(times._Statx) == 256

    def unavailable(*args):
        return 0  # Successful statx without STATX_BTIME is not a birth time.

    monkeypatch.setattr(times, "_STATX", unavailable)
    if not hasattr(path.stat(), "st_birthtime"):
        assert times.filesystem_birthtime(path, fingerprint) is None


def test_real_statx_birthtime_if_filesystem_supports_it(tmp_path):
    path = tmp_path / "one.png"
    path.write_bytes(png_bytes())
    actual = times.filesystem_birthtime(path, file_fingerprint(path.stat()))
    if actual is None:
        pytest.skip("Filesystem or libc does not expose a birth time")
    assert abs(actual - path.stat().st_mtime) < 60


def test_mtime_before_unix_epoch_is_valid_sorting_fallback(tmp_path, monkeypatch):
    path = tmp_path / "old-photo.png"
    data = png_bytes()
    path.write_bytes(data)
    os.utime(path, (-315619200, -315619200))  # 1960-01-01 UTC.
    monkeypatch.setattr(times, "filesystem_birthtime", lambda *args: None)
    selected = times.external_image_times(
        filename=path.name,
        path=path,
        data=data,
        fingerprint=file_fingerprint(path.stat()),
    )
    assert selected["time_source"] == "mtime"
    assert selected["created_at"] == -315619200


def test_invalid_last_fallback_reports_a_file_error_not_stopiteration(
    tmp_path, monkeypatch
):
    path = tmp_path / "invalid-time.png"
    data = png_bytes()
    path.write_bytes(data)
    fingerprint = file_fingerprint(path.stat())
    fingerprint["mtime_ns"] = 253402300800 * 1_000_000_000
    monkeypatch.setattr(times, "filesystem_birthtime", lambda *args: None)
    with pytest.raises(ValueError, match="有效文件时间"):
        times.external_image_times(
            filename=path.name, path=path, data=data, fingerprint=fingerprint
        )
