import os

import pytest

from mapping import (
    MappingError,
    _duration_seconds,
    is_new_release_message,
    is_purge_release_message,
    map_purged_release,
    map_release,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def fixture(name: str) -> str:
    return os.path.join(FIXTURES, name)


def test_maps_release_level_fields():
    release = map_release(fixture("mapping-ern43.xml"))

    assert release.title == "Test Album"
    assert release.artists == ["DJ Test"]
    assert release.label == "Labelwok Records"
    assert release.genre == "House"
    assert release.release_date == "2026-10-15"
    assert release.catalog_number == "LBL001"
    assert release.sender_dpid == "PADPIDA2026101504B"
    assert release.label_dpid == "PADPIDA2026101599Z"
    assert release.sender_name == "Labelworx"


def test_maps_tracks_in_document_order_with_isrc_and_duration():
    release = map_release(fixture("mapping-ern43.xml"))

    assert len(release.tracks) == 2

    first, second = release.tracks
    assert first.title == "Opening Track"
    assert first.artists == ["DJ Test"]
    assert first.isrc == "GBABC2601234"
    assert first.duration_seconds == 210
    assert first.track_number == 1
    assert first.audio_uri == "resources/opening-track.wav"

    assert second.title == "Closing Track"
    assert second.artists == ["DJ Test", "Guest Vocalist"]
    assert second.isrc == "GBABC2601235"
    assert second.duration_seconds == 240
    assert second.track_number == 2
    assert second.audio_uri is None


def test_falls_back_to_icpn_when_no_catalog_number():
    doc = b"""<ern:NewReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43">
      <PartyList></PartyList>
      <ResourceList></ResourceList>
      <ReleaseList>
        <Release>
          <ReleaseId><ICPN>0123456789012</ICPN></ReleaseId>
          <DisplayTitleText>No Catalog Number</DisplayTitleText>
        </Release>
      </ReleaseList>
    </ern:NewReleaseMessage>"""
    path = os.path.join(FIXTURES, "_tmp_icpn.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        release = map_release(path)
        assert release.catalog_number == "0123456789012"
        assert release.sender_dpid is None
        assert release.label_dpid is None
        assert release.sender_name is None
    finally:
        os.remove(path)


def test_raises_when_no_release_found():
    doc = b'<ern:NewReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43"/>'
    path = os.path.join(FIXTURES, "_tmp_empty.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        with pytest.raises(MappingError, match="No Release found"):
            map_release(path)
    finally:
        os.remove(path)


def test_raises_when_release_has_no_title():
    doc = b"""<ern:NewReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43">
      <ReleaseList><Release><ReleaseReference>R1</ReleaseReference></Release></ReleaseList>
    </ern:NewReleaseMessage>"""
    path = os.path.join(FIXTURES, "_tmp_no_title.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        with pytest.raises(MappingError, match="no DisplayTitleText"):
            map_release(path)
    finally:
        os.remove(path)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("PT3M30S", 210),
        ("PT4M0S", 240),
        ("PT1H2M3S", 3723),
        ("PT45S", 45),
        ("PT0.5S", 0),
        ("P", None),
        ("", None),
        (None, None),
        ("not-a-duration", None),
    ],
)
def test_duration_seconds_parses_iso8601(value, expected):
    assert _duration_seconds(value) == expected


def test_is_new_release_message_true_for_new_release_message():
    assert is_new_release_message(fixture("mapping-ern43.xml")) is True


def test_is_new_release_message_false_for_purge_release_message():
    # valid-ern-43-purge.xml is a PurgeReleaseMessage (takedown) fixture
    # shared with test_validator.py - no ReleaseList at all, by design.
    assert is_new_release_message(fixture("valid-ern-43-purge.xml")) is False


def test_is_purge_release_message_true_for_purge_release_message():
    assert is_purge_release_message(fixture("valid-ern-43-purge.xml")) is True


def test_is_purge_release_message_false_for_new_release_message():
    assert is_purge_release_message(fixture("mapping-ern43.xml")) is False


def test_maps_purged_release_grid():
    purge = map_purged_release(fixture("mapping-ern43-purge.xml"))

    assert purge.grid == "A12425678901234125"
    assert purge.catalog_number is None


def test_maps_purged_release_falls_back_to_icpn_when_no_grid():
    doc = b"""<ern:PurgeReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43">
      <PurgedRelease>
        <ReleaseId><ICPN>0123456789012</ICPN></ReleaseId>
      </PurgedRelease>
    </ern:PurgeReleaseMessage>"""
    path = os.path.join(FIXTURES, "_tmp_purge_icpn.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        purge = map_purged_release(path)
        assert purge.catalog_number == "0123456789012"
        assert purge.grid is None
    finally:
        os.remove(path)


def test_raises_when_purged_release_has_no_id():
    with pytest.raises(MappingError, match="no GRid or CatalogNumber/ICPN"):
        map_purged_release(fixture("valid-ern-43-purge.xml"))


def test_raises_when_no_purged_release_found():
    doc = b'<ern:PurgeReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43"/>'
    path = os.path.join(FIXTURES, "_tmp_empty_purge.xml")
    with open(path, "wb") as f:
        f.write(doc)
    try:
        with pytest.raises(MappingError, match="No PurgedRelease found"):
            map_purged_release(path)
    finally:
        os.remove(path)
