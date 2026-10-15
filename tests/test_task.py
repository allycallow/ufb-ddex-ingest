import os

import pytest

from mapping import MappingError
from task import map_delivery_if_possible

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
