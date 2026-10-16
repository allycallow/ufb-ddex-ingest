import os

import pytest

import task
from mapping import MappingError
from task import _stage_cover_art, _with_resource_locations, map_delivery_if_possible

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def test_maps_a_new_release_message_for_ern_43():
    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/43"
    )

    assert result is not None
    assert result["delivery_type"] == "new_release"
    assert result["title"] == "Test Album"
    assert result["tracks"][0]["title"] == "Opening Track"


def test_skips_for_non_ern_43_namespace():
    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/382"
    )

    assert result is None


def test_maps_a_purge_release_message_for_ern_43():
    result = map_delivery_if_possible(
        fixture("mapping-ern43-purge.xml"), "http://ddex.net/xml/ern/43"
    )

    assert result == {
        "delivery_type": "takedown",
        "catalog_number": None,
        "grid": "A12425678901234125",
    }


def test_raises_mapping_error_for_a_new_release_message_missing_a_title():
    doc = b"""<ern:NewReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43">
      <ReleaseList><Release><ReleaseReference>R1</ReleaseReference></Release></ReleaseList>
    </ern:NewReleaseMessage>"""
    path = os.path.join(FIXTURES, "_tmp_no_title_task.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        with pytest.raises(MappingError):
            map_delivery_if_possible(path, "http://ddex.net/xml/ern/43")
    finally:
        os.remove(path)


def test_raises_mapping_error_for_a_purge_release_message_missing_an_id():
    result_path = fixture("valid-ern-43-purge.xml")
    with pytest.raises(MappingError):
        map_delivery_if_possible(result_path, "http://ddex.net/xml/ern/43")


def test_new_release_tracks_get_resolved_audio_locations():
    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/43"
    )
    result = _with_resource_locations(
        result, "test-bucket", "partners/labelworx/x/manifest.xml", None
    )

    first, second = result["tracks"]
    assert first["audio_bucket"] == "test-bucket"
    assert first["audio_key"] == "partners/labelworx/x/resources/opening-track.wav"
    assert "audio_bucket" not in second
    assert "audio_key" not in second


def test_new_release_cover_art_gets_staged_into_the_media_bucket(monkeypatch):
    staged = {}

    def fake_stage(media_bucket, delivery_bucket, delivery_key):
        staged["args"] = (media_bucket, delivery_bucket, delivery_key)
        return "pending-ddex-covers/fake-uuid.jpg"

    monkeypatch.setattr(task, "_stage_cover_art", fake_stage)

    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/43"
    )
    result = _with_resource_locations(
        result, "test-bucket", "partners/labelworx/x/manifest.xml", "media-bucket"
    )

    assert staged["args"] == (
        "media-bucket",
        "test-bucket",
        "partners/labelworx/x/resources/cover.jpg",
    )
    assert result["cover_art_bucket"] == "media-bucket"
    assert result["cover_art_key"] == "pending-ddex-covers/fake-uuid.jpg"


def test_cover_art_staging_failure_leaves_it_unresolved(monkeypatch):
    monkeypatch.setattr(task, "_stage_cover_art", lambda *a: None)

    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/43"
    )
    result = _with_resource_locations(
        result, "test-bucket", "partners/labelworx/x/manifest.xml", "media-bucket"
    )

    assert "cover_art_bucket" not in result
    assert "cover_art_key" not in result


def test_with_resource_locations_skips_cover_art_when_no_media_bucket_configured():
    result = map_delivery_if_possible(
        fixture("mapping-ern43.xml"), "http://ddex.net/xml/ern/43"
    )
    result = _with_resource_locations(
        result, "test-bucket", "partners/labelworx/x/manifest.xml", None
    )

    assert "cover_art_bucket" not in result
    assert "cover_art_key" not in result


def test_with_resource_locations_skips_externally_hosted_cover_art():
    mapped_delivery = {
        "delivery_type": "new_release",
        "tracks": [],
        "cover_art_uri": "https://partner.example.com/cover.jpg",
    }
    result = _with_resource_locations(
        mapped_delivery, "test-bucket", "x/manifest.xml", "media-bucket"
    )

    assert "cover_art_bucket" not in result
    assert "cover_art_key" not in result


def test_with_resource_locations_is_a_no_op_when_no_cover_art():
    mapped_delivery = {"delivery_type": "new_release", "tracks": []}
    result = _with_resource_locations(
        mapped_delivery, "test-bucket", "x/manifest.xml", "media-bucket"
    )

    assert "cover_art_bucket" not in result
    assert "cover_art_key" not in result


def test_with_resource_locations_skips_externally_hosted_audio():
    mapped_delivery = {
        "delivery_type": "new_release",
        "tracks": [{"audio_uri": "https://partner.example.com/track.wav"}],
    }
    result = _with_resource_locations(
        mapped_delivery, "test-bucket", "x/manifest.xml", None
    )

    assert "audio_bucket" not in result["tracks"][0]
    assert "audio_key" not in result["tracks"][0]


def test_with_resource_locations_is_a_no_op_for_a_takedown():
    mapped_delivery = {"delivery_type": "takedown", "catalog_number": "LBL001"}
    result = _with_resource_locations(
        mapped_delivery, "test-bucket", "x/manifest.xml", None
    )

    assert result == mapped_delivery


class _FakeS3:
    def __init__(self, *, get_object_result=None, get_object_error=None):
        self._get_object_result = get_object_result
        self._get_object_error = get_object_error
        self.put_calls = []

    def get_object(self, Bucket, Key):
        if self._get_object_error is not None:
            raise self._get_object_error
        return self._get_object_result

    def put_object(self, **kwargs):
        self.put_calls.append(kwargs)


class _FakeBody:
    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data


def test_stage_cover_art_copies_bytes_from_delivery_to_media_bucket():
    fake_s3 = _FakeS3(get_object_result={"Body": _FakeBody(b"fake-jpeg-bytes")})

    staged_key = _stage_cover_art(
        "media-bucket",
        "delivery-bucket",
        "partners/labelworx/x/resources/cover.jpg",
        s3_client=fake_s3,
    )

    assert staged_key is not None
    assert staged_key.startswith("pending-ddex-covers/")
    assert staged_key.endswith(".jpg")
    assert len(fake_s3.put_calls) == 1
    put_call = fake_s3.put_calls[0]
    assert put_call["Bucket"] == "media-bucket"
    assert put_call["Key"] == staged_key
    assert put_call["Body"] == b"fake-jpeg-bytes"


def test_stage_cover_art_returns_none_on_failure():
    fake_s3 = _FakeS3(get_object_error=Exception("NoSuchKey"))

    staged_key = _stage_cover_art(
        "media-bucket",
        "delivery-bucket",
        "partners/labelworx/x/resources/cover.jpg",
        s3_client=fake_s3,
    )

    assert staged_key is None
