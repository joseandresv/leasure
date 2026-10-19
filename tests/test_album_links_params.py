"""Spotify album links carry name and artist; the album detail renders notices."""
import html as html_lib
import json
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)

SPOTIFY_ALBUM = {"name": "Rock & Roll \"Hits\"", "artist": "AC/DC, Guest", "image_url": None,
                 "year": 1980, "total_tracks": 10, "sources": [{"provider": "spotify", "id": "4uLU6hMCjMI75M1A2tKUQC"}]}
ENCODED_QUERY = "?name=Rock+%26+Roll+%22Hits%22&artist=AC%2FDC%2C+Guest"


def _hx_gets(markup: str) -> list[str]:
    return [html_lib.unescape(url) for url in re.findall(r'hx-get="([^"]*)"', markup)]


def _detail(**context) -> str:
    base = {"album": {"name": "Hot Fuss", "artist": "The Killers"}, "tracks": [],
            "provider": "spotify", "album_id": "abc", "is_playlist": False}
    return env.get_template("partials/music_album_detail.html").render(**{**base, **context})


def test_album_banner_link_carries_name_and_artist():
    markup = env.get_template("partials/music_albums.html").render(albums=[SPOTIFY_ALBUM])

    assert "/api/music/album/spotify/4uLU6hMCjMI75M1A2tKUQC" + ENCODED_QUERY in _hx_gets(markup)


def test_youtube_album_link_has_no_query():
    album = {**SPOTIFY_ALBUM, "sources": [{"provider": "youtube", "id": "MPREb_x"}]}

    markup = env.get_template("partials/music_albums.html").render(albums=[album])

    assert "/api/music/album/youtube/MPREb_x" in _hx_gets(markup)


def test_search_result_album_link_carries_name_and_artist():
    markup = env.get_template("partials/search_results.html").render(
        results={"albums": [SPOTIFY_ALBUM], "tracks": [], "artists": []}, query="x")

    assert "/api/music/album/spotify/4uLU6hMCjMI75M1A2tKUQC" + ENCODED_QUERY in _hx_gets(markup)


@pytest.mark.asyncio
async def test_deck_album_button_carries_album_and_album_artist(client, monkeypatch):
    from routers import music

    track = {"id": "r1", "name": "Mr. Brightside", "artist": "The Killers, Guest", "album": "Hot Fuss",
             "album_artist": "The Killers", "duration_ms": 222_000, "image_url": "",
             "sources": [{"provider": "spotify", "id": "t1", "uri": "spotify:track:t1"}],
             "album_ref": {"provider": "spotify", "id": "4uLU6hMCjMI75M1A2tKUQC"}}
    monkeypatch.setattr(music, "get_unified_recent", lambda limit, refresh: [track])

    resp = await client.get("/api/music/deck")

    assert ("/api/music/album/spotify/4uLU6hMCjMI75M1A2tKUQC?name=Hot+Fuss&artist=The+Killers"
            in _hx_gets(resp.text))


def test_album_detail_renders_notice_as_status():
    markup = _detail(notice="Spotify is rate-limiting Leasure <now>")

    assert '<p class="detail-notice" role="status">Spotify is rate-limiting Leasure &lt;now&gt;</p>' in markup


def test_album_detail_without_notice_has_none():
    assert "detail-notice" not in _detail()


def _actions_tag(markup: str) -> str:
    return re.search(r'<div class="album-actions"[^>]*>', markup).group(0)


def test_spotify_detail_still_offers_download_all():
    track = {"name": "T", "sources": [{"provider": "spotify", "id": "t", "uri": "spotify:track:t"}],
             "download_status": None, "db": None}

    markup = _detail(tracks=[track])

    assert "Download All" in markup and "dl-btn" in markup


def _row(status: str | None) -> dict:
    return {"name": "T", "sources": [{"provider": "youtube", "id": "v"}], "download_status": status, "db": None}


def test_album_with_every_track_downloaded_or_queued_hides_download_all():
    markup = _detail(provider="youtube", tracks=[_row("done"), _row("pending"), _row("downloading")])

    assert "Download All" not in markup
    assert 'id="dl-format-detail"' in markup and " hidden" in _actions_tag(markup)


def test_album_with_a_failed_track_still_offers_download_all():
    markup = _detail(provider="youtube", tracks=[_row("done"), _row("error")])

    assert "Download All" in markup and "hidden" not in _actions_tag(markup)


def test_youtube_stand_in_download_all_names_the_spotify_album():
    markup = _detail(provider="youtube", album_id="MPREb_x", tracks=[_row(None)],
                     spotify_album={"id": "4uLU6hMCjMI75M1A2tKUQC", "name": "Rock & Roll 'Hits'"})

    hx_vals = re.search(r"hx-post=\"/api/music/download/album/youtube/MPREb_x\"\s+hx-vals='([^']*)'", markup)
    assert json.loads(html_lib.unescape(hx_vals.group(1))) == {
        "spotify_album_id": "4uLU6hMCjMI75M1A2tKUQC", "spotify_album_name": "Rock & Roll 'Hits'"}


def test_plain_album_download_all_sends_no_stand_in():
    markup = _detail(provider="youtube", tracks=[_row(None)])

    assert "spotify_album_id" not in markup


def test_the_server_side_album_url_carries_the_name_for_youtube_too():
    from services.album_view import album_detail_url

    assert album_detail_url("youtube", "MPREb_x", "Hot Fuss", "The Killers") == (
        "/api/music/album/youtube/MPREb_x?name=Hot+Fuss&artist=The+Killers")
