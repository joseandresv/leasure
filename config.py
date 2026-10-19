from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Spotify
    spotify_client_id: str = ""
    spotify_client_secret: str = ""
    spotify_redirect_uri: str = "http://127.0.0.1:8642/api/spotify/callback"

    # Google OAuth (for YouTube history)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = "http://127.0.0.1:8642/api/youtube/oauth/callback"

    # Qobuz (FLAC). The login lives in data/qobuz_session.json (Qobuz card on Home), never in .env.
    qobuz_max_format: int = 27            # 27 = up to 24/192, 7 = up to 24/96, 6 = 16/44.1
    qobuz_max_downloads_per_day: int = 100
    qobuz_api_calls_per_minute: int = 20
    qobuz_check_query: str = "Miles Davis So What"
    qobuz_upgrade_keep_days: int = 30     # old files of an upgrade stay in data/upgrades/ for Revert

    # Paths
    library_dir: Path = Path("./library")
    download_dir: Path = Path("./downloads")
    data_dir: Path = Path("./data")

    # Download defaults
    # Browser to extract YouTube Music cookies from (chrome | firefox | edge | brave ...).
    # On native Windows, Chrome 127+ app-bound encryption can block extraction — use firefox there.
    cookie_browser: str = "chrome"
    # Netscape cookies.txt yt-dlp authenticates with; defaults to data_dir/cookies.txt.
    cookie_file: Path | None = None
    # Track the Premium gate probes. Must be public on music.youtube.com; when it is taken
    # down the check falls through to cookies.PREMIUM_CHECK_FALLBACK_IDS.
    premium_check_video_id: str = "dQw4w9WgXcQ"
    default_format: str = "mp3"  # native | mp3 | flac_lossless
    mp3_bitrate: int = 320
    # Ceiling on authenticated YouTube downloads per day (counter in data/yt_daily.json).
    yt_max_downloads_per_day: int = 50
    max_concurrent_downloads: int = 1
    # False for throwaway/test servers: nothing is downloaded and pending tracks are not re-queued.
    download_worker_enabled: bool = True
    artwork_size: int = 500
    # Look downloaded albums up on MusicBrainz, then Deezer, to show their full tracklists.
    album_info_lookups: bool = True
    # ID3v2 minor version for MP3 tags: 3 (v2.3) is what embedded players read reliably, 4 is v2.4.
    id3_version: int = 3

    # Device
    device_music_folder: str = "MUSIC"
    device_playlist_folder: str = "PLAYLIST"

    # Server
    host: str = "127.0.0.1"
    port: int = 8642
    # Host header values the server answers (DNS-rebinding guard). Add a LAN name here to reach Leasure from it.
    allowed_hosts: list[str] = ["127.0.0.1", "localhost"]

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("id3_version")
    @classmethod
    def _supported_id3_version(cls, value: int) -> int:
        if value not in (3, 4):
            raise ValueError("ID3_VERSION must be 3 (ID3v2.3) or 4 (ID3v2.4)")
        return value

    @field_validator("qobuz_max_format")
    @classmethod
    def _known_qobuz_format(cls, value: int) -> int:
        if value not in (6, 7, 27):
            raise ValueError("QOBUZ_MAX_FORMAT must be 27 (24-bit/192 kHz), 7 (24-bit/96 kHz) or 6 (16-bit/44.1 kHz)")
        return value

    @field_validator("qobuz_api_calls_per_minute")
    @classmethod
    def _gentle_qobuz_rate(cls, value: int) -> int:
        if not 1 <= value <= 40:
            raise ValueError("QOBUZ_API_CALLS_PER_MINUTE must be between 1 and 40")
        return value

    @field_validator("qobuz_max_downloads_per_day")
    @classmethod
    def _non_negative_cap(cls, value: int) -> int:
        if value < 0:
            raise ValueError("QOBUZ_MAX_DOWNLOADS_PER_DAY cannot be negative")
        return value

    @field_validator("qobuz_upgrade_keep_days")
    @classmethod
    def _keep_backups_a_day(cls, value: int) -> int:
        if value < 1:
            raise ValueError("QOBUZ_UPGRADE_KEEP_DAYS must be at least 1")
        return value

    @field_validator("allowed_hosts")
    @classmethod
    def _some_host(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("ALLOWED_HOSTS needs at least one host name")
        return value


settings = Settings()

# Ensure directories exist
for d in [settings.library_dir, settings.download_dir, settings.data_dir]:
    d.mkdir(parents=True, exist_ok=True)
