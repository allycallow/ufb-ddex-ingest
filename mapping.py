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

# The only namespace map_release() understands - see the module docstring.
ERN_43_NAMESPACE = "http://ddex.net/xml/ern/43"


@dataclass
class MappedTrack:
    title: str
    artists: list[str]
    isrc: str | None
    duration_seconds: int | None
    track_number: int
    # The delivered audio file's File/URI, relative to the manifest's own
    # S3 directory (same convention checksum.py's _resource_refs already
    # resolves) - task.py turns this into an absolute bucket/key so
    # DdexDeliveryWorkflow can start the encoding pipeline on it. None if
    # this track carries no DeliveryFile, or an externally-hosted one
    # (nothing in this bucket to encode).
    audio_uri: str | None = None


@dataclass
class MappedPurge:
    # Identifies which existing Release to take down - a PurgeReleaseMessage
    # carries only PurgedRelease/ReleaseId (GRid and/or ICPN), never a title
    # or any other descriptive field, so there's nothing else to map here.
    catalog_number: str | None
    grid: str | None


@dataclass
class MappedRelease:
    title: str
    artists: list[str]
    label: str | None
    genre: str | None
    release_date: str | None
    catalog_number: str | None
    # The delivering partner's own DPID (from MessageHeader/MessageSender)
    # and the label's own DPID (from its PartyList entry, when given) -
    # ufb/backend's DdexDeliveryController accepts and stores both
    # (sender_dpid on DdexDelivery, label_dpid on Label) but nothing
    # populated them until this field existed.
    sender_dpid: str | None = None
    label_dpid: str | None = None
    # The delivering partner's display name, from the same MessageSender -
    # optional per the schema, so backend falls back to the DPID as
    # Distributor.name when a sender omits it.
    sender_name: str | None = None
    # The release's front cover art File/URI, relative to the manifest's
    # own S3 directory (same convention as MappedTrack.audio_uri) - None if
    # this delivery links no FrontCoverImage, or an externally-hosted one.
    cover_art_uri: str | None = None
    tracks: list[MappedTrack] = field(default_factory=list)


class MappingError(Exception):
    """Raised when a manifest is missing a field this mapper requires."""


def is_new_release_message(manifest_path: str) -> bool:
    """True if the manifest's root element is NewReleaseMessage.

    A PurgeReleaseMessage (takedown) has no ReleaseList at all by design -
    map_release() would raise MappingError on one, correctly, but that's a
    different situation from a NewReleaseMessage that's missing a title.
    Callers should check this first and skip map_release() entirely for
    anything that isn't a NewReleaseMessage, rather than treating "no
    release found" as a mapping failure.
    """
    doc = etree.parse(manifest_path)
    return etree.QName(doc.getroot()).localname == "NewReleaseMessage"


def is_purge_release_message(manifest_path: str) -> bool:
    """True if the manifest's root element is PurgeReleaseMessage (a
    takedown) - see map_purged_release() for what's mapped from one."""
    doc = etree.parse(manifest_path)
    return etree.QName(doc.getroot()).localname == "PurgeReleaseMessage"


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


def _party_dpids(doc) -> dict[str, str]:
    """Maps each Party's PartyReference to its DPID, when it has one.

    Party's PartyId is type DetailedPartyId (ISNI/DPID/etc sub-elements)
    in both of its two allowed shapes (PartyId-only, or PartyName+optional
    PartyId) - so a plain PartyId/DPID lookup works regardless of which
    shape a given Party actually used.
    """
    dpids = {}
    for party in doc.findall(".//PartyList/Party"):
        ref = _text(party.find("PartyReference"))
        dpid = _text(party.find("PartyId/DPID"))
        if ref and dpid:
            dpids[ref] = dpid
    return dpids


def _sender_dpid(doc) -> str | None:
    """The delivering partner's own DPID, from MessageHeader/MessageSender -
    a plain PartyId element here (not DetailedPartyId - MessageSender uses
    MessagingPartyWithoutCode, a simpler composite than PartyList's Party)."""
    return _text(doc.find(".//MessageHeader/MessageSender/PartyId"))


def _audio_uri(sr) -> str | None:
    """The delivered audio file's own File/URI for this SoundRecording -
    DeliveryFile is maxOccurs="unbounded" (multiple bitrate variants are
    allowed), but like release_el/Release elsewhere in this module, only
    the first one found is mapped."""
    return _text(sr.find("TechnicalDetails/DeliveryFile/File/URI"))


def _cover_art_uri(doc, release_el) -> str | None:
    """The release's front cover art File/URI, if this delivery includes
    one. Unlike a track's audio file (a direct child of its own
    SoundRecording), cover art is only ever communicated as a reference:
    Release/ResourceGroup/LinkedReleaseResourceReference holds an Image
    resource's own ResourceReference, and that Image (in ResourceList,
    alongside the SoundRecordings) carries the actual File/URI. Only the
    first ResourceGroup is read, and only an Image typed FrontCoverImage
    is matched - a delivery can link other image types (booklets, etc.)
    via the same mechanism, and multiple Images could theoretically share
    this release's ResourceGroup.
    """
    resource_group = release_el.find("ResourceGroup")
    if resource_group is None:
        return None

    front_covers = {
        ref: image_el
        for image_el in doc.findall(".//ResourceList/Image")
        if _text(image_el.find("Type")) == "FrontCoverImage"
        and (ref := _text(image_el.find("ResourceReference")))
    }

    for linked_ref_el in resource_group.findall("LinkedReleaseResourceReference"):
        image_el = front_covers.get(_text(linked_ref_el))
        if image_el is not None:
            return _text(image_el.find("TechnicalDetails/File/URI"))

    return None


def _sender_name(doc) -> str | None:
    """The delivering partner's display name, from the same MessageSender -
    PartyName is optional on MessagingPartyWithoutCode (a sender may give
    only its DPID), so this is best-effort like the rest of the mapping."""
    return _text(doc.find(".//MessageHeader/MessageSender/PartyName/FullName"))


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
    party_dpids = _party_dpids(doc)

    release_el = doc.find(".//ReleaseList/Release")
    if release_el is None:
        raise MappingError("No Release found in ReleaseList")

    title = _text(release_el.find("DisplayTitleText"))
    if not title:
        raise MappingError("Release has no DisplayTitleText")

    label_ref = _text(release_el.find("ReleaseLabelReference"))
    label = party_names.get(label_ref) if label_ref else None
    label_dpid = party_dpids.get(label_ref) if label_ref else None

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
                audio_uri=_audio_uri(sr),
            )
        )

    return MappedRelease(
        title=title,
        artists=_artists(release_el),
        label=label,
        genre=_text(release_el.find("Genre/GenreText")),
        release_date=_text(release_el.find("ReleaseDate")),
        catalog_number=catalog_number,
        sender_dpid=_sender_dpid(doc),
        label_dpid=label_dpid,
        sender_name=_sender_name(doc),
        cover_art_uri=_cover_art_uri(doc, release_el),
        tracks=tracks,
    )


def map_purged_release(manifest_path: str) -> MappedPurge:
    """Maps a PurgeReleaseMessage's PurgedRelease into the identifiers
    UFB's catalog needs to find the Release to take down.

    Raises MappingError if PurgedRelease has neither a GRid nor an
    ICPN/CatalogNumber - there's then no way to identify which Release
    this delivery is even about.
    """
    doc = etree.parse(manifest_path)

    purged_el = doc.find(".//PurgedRelease")
    if purged_el is None:
        raise MappingError("No PurgedRelease found")

    catalog_number = _text(purged_el.find("ReleaseId/CatalogNumber")) or _text(
        purged_el.find("ReleaseId/ICPN")
    )
    grid = _text(purged_el.find("ReleaseId/GRid"))

    if not catalog_number and not grid:
        raise MappingError("PurgedRelease has no GRid or CatalogNumber/ICPN")

    return MappedPurge(catalog_number=catalog_number, grid=grid)
