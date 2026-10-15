"""Maps a validated ERN manifest to the fields UFB's catalog backend needs
to create a draft Release/Track/Artist/Label (see ufb/backend/upfrontbeats/
models/release.py and track.py).

Scope, deliberately: ERN 4.3 only - 3.8.2's ReleaseList/PartyList shapes
differ enough to need their own pass, and XSD validation/checksum
verification already cover both versions structurally without needing this.
Only maps fields that have a direct home in Django's models: release
title/artists/label/genre/release_date/catalog_number, and per-track
title/artists/isrc/duration.

Deliberately NOT covered, because nothing in the Django schema models them
(or it would need a design decision beyond "map the field that exists"):
deals/territories, parental warnings, version types, compilation flags, and
true multi-disc ResourceGroup sequencing - track order instead falls back
to each SoundRecording's position in ResourceList's document order, which
is correct for the common single-disc case but isn't a real read of
ResourceGroup's sequencing for multi-disc releases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

_DURATION_RE = re.compile(r"P(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?")


@dataclass
class MappedTrack:
    title: str
    artists: list[str]
    isrc: str | None
    duration_seconds: int | None
    track_number: int


@dataclass
class MappedRelease:
    title: str
    artists: list[str]
    label: str | None
    genre: str | None
    release_date: str | None
    catalog_number: str | None
    tracks: list[MappedTrack] = field(default_factory=list)


class MappingError(Exception):
    """Raised when a manifest is missing a field this mapper requires."""


def _text(el) -> str | None:
    if el is None or el.text is None:
        return None
    text = el.text.strip()
    return text or None


def _artists(el) -> list[str]:
    return [name for n in el.findall("DisplayArtistName") if (name := _text(n))]


def _party_names(doc) -> dict[str, str]:
    """Maps each Party's PartyReference (e.g. "P1") to its FullName, for
    resolving ReleaseLabelReference - the label is only ever communicated
    as a reference into PartyList, never as a plain string."""
    names = {}
    for party in doc.findall(".//PartyList/Party"):
        ref = _text(party.find("PartyReference"))
        full_name = _text(party.find("PartyName/FullName"))
        if ref and full_name:
            names[ref] = full_name
    return names


def _duration_seconds(value: str | None) -> int | None:
    """Parses an xs:duration (PT[nH][nM][nS]) - the only format ERN uses
    here. Returns None on anything that doesn't match rather than raising -
    a missing/odd duration shouldn't block the rest of the mapping."""
    if not value:
        return None
    match = _DURATION_RE.fullmatch(value)
    if not match or value in ("P", "PT"):
        return None
    hours, minutes, seconds = match.groups()
    total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + float(seconds or 0)
    return int(total)


def map_release(manifest_path: str) -> MappedRelease:
    """Maps a validated ERN 4.3 NewReleaseMessage into UFB's catalog fields.

    Maps the first Release in ReleaseList - DDEX allows multiple Releases
    per message (e.g. a grouped delivery), but a delivery conventionally
    carries one; supporting more than one Release per message isn't handled
    yet.

    Raises MappingError if the manifest is missing a Release or its title -
    everything else is best-effort and left as None rather than failing the
    whole mapping.
    """
    doc = etree.parse(manifest_path)
    party_names = _party_names(doc)

    release_el = doc.find(".//ReleaseList/Release")
    if release_el is None:
        raise MappingError("No Release found in ReleaseList")

    title = _text(release_el.find("DisplayTitleText"))
    if not title:
        raise MappingError("Release has no DisplayTitleText")

    label_ref = _text(release_el.find("ReleaseLabelReference"))
    label = party_names.get(label_ref) if label_ref else None

    catalog_number = _text(release_el.find("ReleaseId/CatalogNumber")) or _text(
        release_el.find("ReleaseId/ICPN")
    )

    tracks = []
    for i, sr in enumerate(doc.findall(".//ResourceList/SoundRecording"), start=1):
        track_title = _text(sr.find("DisplayTitleText"))
        if not track_title:
            continue
        tracks.append(
            MappedTrack(
                title=track_title,
                artists=_artists(sr),
                isrc=_text(sr.find("SoundRecordingEdition/ResourceId/ISRC")),
                duration_seconds=_duration_seconds(_text(sr.find("Duration"))),
                track_number=i,
            )
        )

    return MappedRelease(
        title=title,
        artists=_artists(release_el),
        label=label,
        genre=_text(release_el.find("Genre/GenreText")),
        release_date=_text(release_el.find("ReleaseDate")),
        catalog_number=catalog_number,
        tracks=tracks,
    )
