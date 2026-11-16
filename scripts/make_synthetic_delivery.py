"""Builds a synthetic ERN 4.3 NewReleaseMessage delivery for the end-to-end
dry run (see docs/e2e-dry-run.md): a manifest plus the audio and cover art
it references, with real checksums.

    python scripts/make_synthetic_delivery.py OUT_DIR [--tag NAME]
        [--catalog-number NUM] [--sender-dpid DPID] [--takedown] [--corrupt]

Writes OUT_DIR/<tag>/<tag>.xml and OUT_DIR/<tag>/resources/*. Upload the
whole <tag> folder under partners/<dpid>/ with the manifest LAST.

--takedown writes a PurgeReleaseMessage for the catalog number instead.
--corrupt tampers with a resource after hashing it, so the delivery must be
rejected. An update is just a second delivery with the same
--catalog-number and a different --tag.

The audio is a short generated tone (valid WAV, so audio-processing can
really encode it) and the cover is a 1x1 JPEG. Everything is deterministic
for a given --tag, so rebuilding gives the same checksums.
"""

import argparse
import base64
import hashlib
import io
import math
import os
import struct
import wave
from xml.sax.saxutils import escape

# Smallest valid JPEG (1x1 white pixel).
_JPEG_1X1 = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////"
    "////////////////////////////////////////////////////wgALCAABAAEBAREA"
    "/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA="
)

TRACKS = [
    # (ResourceReference, title, ISRC, seconds, frequency)
    ("A1", "Synthetic Opening", "GBSYN2600001", 3, 440.0),
    ("A2", "Synthetic Closing", "GBSYN2600002", 3, 554.37),
]


def make_wav(seconds: int, frequency: float) -> bytes:
    rate = 44100
    frames = b"".join(
        struct.pack("<h", int(12000 * math.sin(2 * math.pi * frequency * i / rate)))
        for i in range(rate * seconds)
    )
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _file(uri: str, data: bytes) -> str:
    return (
        f"<File><URI>{escape(uri)}</URI><HashSum>"
        f"<Algorithm>MD5</Algorithm>"
        f"<HashSumValue>{md5(data)}</HashSumValue></HashSum></File>"
    )


def build_manifest(
    tag: str, sender_dpid: str, recipient_dpid: str, catalog_number: str, files: dict
) -> str:
    sound_recordings = "".join(f"""
    <SoundRecording>
      <ResourceReference>{ref}</ResourceReference>
      <Type>MusicalWorkSoundRecording</Type>
      <SoundRecordingEdition>
        <Type>NonImmersiveEdition</Type>
        <ResourceId><ISRC>{isrc}</ISRC></ResourceId>
        <TechnicalDetails>
          <TechnicalResourceDetailsReference>T{ref}</TechnicalResourceDetailsReference>
          <DeliveryFile>
            <Type>AudioFile</Type>
            <AudioCodecType>PCM</AudioCodecType>
            {_file(f"resources/{ref}.wav", files[f"{ref}.wav"])}
          </DeliveryFile>
        </TechnicalDetails>
      </SoundRecordingEdition>
      <DisplayTitleText>{escape(title)}</DisplayTitleText>
      <DisplayTitle><TitleText>{escape(title)}</TitleText></DisplayTitle>
      <DisplayArtistName>Synthetic Artist</DisplayArtistName>
      <DisplayArtist SequenceNumber="1">
        <ArtistPartyReference>PArtist</ArtistPartyReference>
        <DisplayArtistRole>MainArtist</DisplayArtistRole>
      </DisplayArtist>
      <Duration>PT{seconds}S</Duration>
      <ParentalWarningType>NotExplicit</ParentalWarningType>
    </SoundRecording>""" for ref, title, isrc, seconds, _ in TRACKS)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<ern:NewReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43" AvsVersionId="4" LanguageAndScriptCode="en">
  <MessageHeader>
    <MessageId>{tag}</MessageId>
    <MessageSender><PartyId>{sender_dpid}</PartyId></MessageSender>
    <MessageRecipient><PartyId>{recipient_dpid}</PartyId></MessageRecipient>
    <MessageCreatedDateTime>2026-01-01T00:00:00Z</MessageCreatedDateTime>
  </MessageHeader>
  <PartyList>
    <Party>
      <PartyReference>PLabel</PartyReference>
      <PartyName><FullName>Synthetic Label</FullName></PartyName>
      <PartyId><DPID>{sender_dpid}</DPID></PartyId>
    </Party>
    <Party>
      <PartyReference>PArtist</PartyReference>
      <PartyName><FullName>Synthetic Artist</FullName></PartyName>
    </Party>
  </PartyList>
  <ResourceList>{sound_recordings}
    <Image>
      <ResourceReference>A3</ResourceReference>
      <Type>FrontCoverImage</Type>
      <ResourceId><ProprietaryId Namespace="Synthetic">{tag}-cover</ProprietaryId></ResourceId>
      <TechnicalDetails>
        <TechnicalResourceDetailsReference>TA3</TechnicalResourceDetailsReference>
        {_file("resources/cover.jpg", files["cover.jpg"])}
      </TechnicalDetails>
    </Image>
  </ResourceList>
  <ReleaseList>
    <Release>
      <ReleaseReference>R1</ReleaseReference>
      <ReleaseType>Album</ReleaseType>
      <ReleaseId><CatalogNumber Namespace="Synthetic">{catalog_number}</CatalogNumber></ReleaseId>
      <DisplayTitleText>Synthetic Release {tag}</DisplayTitleText>
      <DisplayTitle><TitleText>Synthetic Release {tag}</TitleText></DisplayTitle>
      <DisplayArtistName>Synthetic Artist</DisplayArtistName>
      <DisplayArtist SequenceNumber="1">
        <ArtistPartyReference>PArtist</ArtistPartyReference>
        <DisplayArtistRole>MainArtist</DisplayArtistRole>
      </DisplayArtist>
      <ReleaseLabelReference>PLabel</ReleaseLabelReference>
      <Genre><GenreText>House</GenreText></Genre>
      <ReleaseDate>2026-01-01</ReleaseDate>
      <ParentalWarningType>NotExplicit</ParentalWarningType>
      <ResourceGroup>
        <LinkedReleaseResourceReference>A3</LinkedReleaseResourceReference>
      </ResourceGroup>
    </Release>
  </ReleaseList>
</ern:NewReleaseMessage>
"""


def write_delivery(
    out_dir: str, tag: str, sender_dpid: str, recipient_dpid: str, catalog_number: str
) -> str:
    folder = os.path.join(out_dir, tag)
    os.makedirs(os.path.join(folder, "resources"), exist_ok=True)

    files = {f"{ref}.wav": make_wav(s, hz) for ref, _, _, s, hz in TRACKS}
    files["cover.jpg"] = _JPEG_1X1
    for name, data in files.items():
        with open(os.path.join(folder, "resources", name), "wb") as f:
            f.write(data)

    manifest = os.path.join(folder, f"{tag}.xml")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write(build_manifest(tag, sender_dpid, recipient_dpid, catalog_number, files))
    return manifest


def write_takedown(
    out_dir: str, tag: str, sender_dpid: str, recipient_dpid: str, catalog_number: str
) -> str:
    """A PurgeReleaseMessage for the release a previous delivery created -
    no resources, just the identifier the backend matches on."""
    folder = os.path.join(out_dir, tag)
    os.makedirs(folder, exist_ok=True)
    manifest = os.path.join(folder, f"{tag}.xml")
    with open(manifest, "w", encoding="utf-8") as f:
        f.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<ern:PurgeReleaseMessage xmlns:ern="http://ddex.net/xml/ern/43" AvsVersionId="4" LanguageAndScriptCode="en">
  <MessageHeader>
    <MessageId>{tag}</MessageId>
    <MessageSender><PartyId>{sender_dpid}</PartyId></MessageSender>
    <MessageRecipient><PartyId>{recipient_dpid}</PartyId></MessageRecipient>
    <MessageCreatedDateTime>2026-01-01T00:00:00Z</MessageCreatedDateTime>
  </MessageHeader>
  <PurgedRelease>
    <ReleaseId><CatalogNumber Namespace="Synthetic">{catalog_number}</CatalogNumber></ReleaseId>
  </PurgedRelease>
</ern:PurgeReleaseMessage>
""")
    return manifest


def corrupt_first_resource(manifest: str) -> None:
    """Changes a resource after its checksum was written into the manifest,
    so ddex-ingest must reject the delivery naming that resource."""
    path = os.path.join(os.path.dirname(manifest), "resources", f"{TRACKS[0][0]}.wav")
    with open(path, "ab") as f:
        f.write(b"corrupt")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("out_dir")
    parser.add_argument("--tag", default="synthetic-001")
    parser.add_argument("--sender-dpid", default="PADPIDA2026000001S")
    parser.add_argument("--recipient-dpid", default="PADPIDA2026000002S")
    parser.add_argument("--catalog-number", default="SYN001")
    parser.add_argument(
        "--takedown",
        action="store_true",
        help="write a PurgeReleaseMessage for --catalog-number instead",
    )
    parser.add_argument(
        "--corrupt",
        action="store_true",
        help="tamper with a resource after hashing it, to test rejection",
    )
    args = parser.parse_args()

    write = write_takedown if args.takedown else write_delivery
    manifest = write(
        args.out_dir,
        args.tag,
        args.sender_dpid,
        args.recipient_dpid,
        args.catalog_number,
    )
    if args.corrupt and not args.takedown:
        corrupt_first_resource(manifest)
    print(manifest)


if __name__ == "__main__":
    main()
