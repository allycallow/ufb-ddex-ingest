import os

import pytest

from mapping import MappingError, _duration_seconds, map_release

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


def test_maps_tracks_in_document_order_with_isrc_and_duration():
    release = map_release(fixture("mapping-ern43.xml"))

    assert len(release.tracks) == 2

    first, second = release.tracks
    assert first.title == "Opening Track"
    assert first.artists == ["DJ Test"]
    assert first.isrc == "GBABC2601234"
    assert first.duration_seconds == 210
    assert first.track_number == 1

    assert second.title == "Closing Track"
    assert second.artists == ["DJ Test", "Guest Vocalist"]
    assert second.isrc == "GBABC2601235"
    assert second.duration_seconds == 240
    assert second.track_number == 2


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
