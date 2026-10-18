import time

import pytest
import spotipy

from services import spotify_client


class FailingSpotify:
    def __init__(self, status: int):
        self.status = status

    def playlist(self, *args, **kwargs):
        raise spotipy.SpotifyException(self.status, -1, "refused", headers={"Retry-After": "600"})

    playlist_items = playlist


def _spotify_failing_with(monkeypatch, status: int) -> None:
    monkeypatch.setattr(spotify_client, "_rate_limited_until", 0.0)
    monkeypatch.setattr(spotify_client, "get_client", lambda: FailingSpotify(status))


def test_playlist_fetch_reports_the_spotify_status(monkeypatch):
    _spotify_failing_with(monkeypatch, 403)
    assert spotify_client.get_playlist_tracks("PL1")["error_status"] == 403


@pytest.mark.asyncio
async def test_a_rate_limited_playlist_points_to_youtube_music_playlists(client, db, monkeypatch):
    monkeypatch.setattr(spotify_client, "_rate_limited_until", time.monotonic() + 7200)

    resp = await client.get("/api/music/playlist/spotify/PL1")

    assert "(try again in ~2 h). YouTube Music playlists still work." in resp.text


@pytest.mark.asyncio
async def test_a_private_spotify_playlist_says_why_it_cannot_be_read(client, db, monkeypatch):
    _spotify_failing_with(monkeypatch, 403)

    resp = await client.get("/api/music/playlist/spotify/PL1")

    assert ("This Spotify playlist is private or not available to Leasure (Spotify only lets apps read "
            "playlists you own or follow).") in resp.text
