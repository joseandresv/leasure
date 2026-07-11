"""Shared mapping from a user-facing download format choice to internal
quality + container values. Kept in one place so the routers can't drift."""

# UI format value -> (quality tag, initial container)
#   native         -- keep the original AAC/Opus stream, no re-encode (best fidelity our sources can give)
#   mp3            -- transcode to MP3 320
#   flac           -- transcode a lossy stream into a FLAC container (NOT true lossless)
#   flac_lossless  -- real lossless from Qobuz/Tidal/Bandcamp/Archive (requires setup; falls back to lossy)
_FORMAT_MAP: dict[str, tuple[str, str]] = {
    "native": ("native", "m4a"),
    "mp3": ("mp3_320", "mp3"),
    "flac": ("flac_lossy", "flac"),
    "flac_lossless": ("flac_lossless", "flac"),
}


def resolve_format(fmt: str) -> tuple[str, str]:
    """Return (quality, container) for a UI format value, defaulting to MP3."""
    return _FORMAT_MAP.get(fmt, _FORMAT_MAP["mp3"])
