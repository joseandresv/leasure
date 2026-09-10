"""Shared mapping from a user-facing download format choice to internal
quality + container values. Kept in one place so the routers can't drift."""

# UI format value -> (quality tag, initial container)
#   native         -- keep the original AAC/Opus stream, no re-encode (best fidelity our sources can give)
#   mp3            -- transcode to MP3 320
#   flac_lossless  -- real lossless from Qobuz/Tidal/Bandcamp/Archive (requires setup; fails if unavailable)
# There is deliberately no lossy-FLAC option: a FLAC container filled from a YouTube
# stream claims a fidelity the source never had.
_FORMAT_MAP: dict[str, tuple[str, str]] = {
    "native": ("native", "m4a"),
    "mp3": ("mp3_320", "mp3"),
    "flac_lossless": ("flac_lossless", "flac"),
}


def resolve_format(fmt: str) -> tuple[str, str]:
    """Return (quality, container) for a UI format value.

    Unknown values raise instead of falling back: a stale page asking for a format we
    dropped (lossy `flac`) must not silently get MP3.
    """
    try:
        return _FORMAT_MAP[fmt]
    except KeyError:
        raise ValueError(
            f"Format '{fmt}' is no longer available; choose Best available, MP3 or FLAC lossless"
        ) from None
