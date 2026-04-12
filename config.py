from pathlib import Path

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
    default_format: str = "mp3"  # mp3 | flac | flac_lossless
    mp3_bitrate: int = 320
    max_concurrent_downloads: int = 1
    artwork_size: int = 500

    # Device
    device_music_folder: str = "MUSIC"
    device_playlist_folder: str = "PLAYLIST"

    # Server
    host: str = "127.0.0.1"
    port: int = 8642

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()

# Ensure directories exist
for d in [settings.library_dir, settings.download_dir, settings.data_dir]:
    d.mkdir(parents=True, exist_ok=True)
