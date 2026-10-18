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

    # Lossless services (optional)
    qobuz_email: str = ""
    qobuz_password: str = ""
    tidal_email: str = ""
    tidal_password: str = ""
    deezer_arl: str = ""

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
    # ID3v2 minor version for MP3 tags: 3 (v2.3) is what embedded players read reliably, 4 is v2.4.
    id3_version: int = 3

    # Device
    device_music_folder: str = "MUSIC"
    device_playlist_folder: str = "PLAYLIST"

    # Server
    host: str = "127.0.0.1"
    port: int = 8642

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}

    @field_validator("id3_version")
    @classmethod
    def _supported_id3_version(cls, value: int) -> int:
        if value not in (3, 4):
            raise ValueError("ID3_VERSION must be 3 (ID3v2.3) or 4 (ID3v2.4)")
        return value


settings = Settings()

# Ensure directories exist
for d in [settings.library_dir, settings.download_dir, settings.data_dir]:
    d.mkdir(parents=True, exist_ok=True)
