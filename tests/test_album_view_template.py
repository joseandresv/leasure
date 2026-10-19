"""partials/album_view.html: the local album view rendered from the §7.1 context."""
import html as html_lib
import re
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parent.parent / "templates"
env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=True)

KEY = "a-0123456789abcdef0123"
BASE = f"/api/albums/{KEY}"


def _row(name: str, state: str, position: int, **extra) -> dict:
    row = {"name": name, "artist": "The Killers", "disc_number": 1, "track_number": position,
           "release_position": position, "duration_ms": 200_000, "isrc": None, "state": state,
           "download_status": None, "db": None, "track_id": None, "on_release": True, "match": None,
           "elsewhere": None, "download_url": None}
    return {**row, **extra}


def _local(name: str, position: int, track_id: int, **extra) -> dict:
    db = {"id": track_id, "quality": "aac_256", "format": "m4a", "status": "done", "upgrade_possible": True}
    return _row(name, "local", position, **{"download_status": "done", "db": db, "track_id": track_id,
                                            "match": "isrc", **extra})


def _context(**overrides) -> dict:
    tracks = [
        _local("Jenny Was a Friend of Mine", 1, 11),
        _local("Mr. Brightside", 2, 12),
        _row("Smile Like You Mean It", "missing", 3, download_url=f"{BASE}/download/1/3"),
        _row("Somebody Told Me", "elsewhere", 4, elsewhere={"album": "Direct Hits", "key": "a-ffffffffffffffffffff"}),
        _row("All These Things That I've Done", "queued", 5, download_status="pending"),
        _row("Andy, You're a Star", "error", 6, download_status="error", download_url=f"{BASE}/download/1/6"),
        _local("Glamorous Indie Rock & Roll", 12, 13, on_release=False, match=None),
    ]
    album = {"key": KEY, "kind": "album", "name": "Hot Fuss", "artist": "The Killers", "album_artist": "The Killers",
             "year": 2004, "release_date": "2004-06-07", "label": "Island", "catalog_number": "B0002468-02",
             "barcode": "602498620211", "media_format": "Digital Media", "total_tracks": 11, "total_discs": 1,
             "total_duration_ms": 2_740_000, "local_count": 3, "missing_count": 1, "elsewhere_count": 1,
             "bonus_count": 1, "image_url": f"{BASE}/cover?t=11", "genres": ["rock", "indie rock"]}
    info = {"status": "found", "pending": False, "should_ensure": False, "can_enrich": True, "mismatch": False,
            "pinned": False, "artist_warning": False, "source": "musicbrainz", "source_label": "MusicBrainz",
            "source_url": "https://musicbrainz.org/release/1b2c3d4e-0000-0000-0000-000000000000",
            "confidence": 0.97, "fetched_at": "2026-10-18T10:00:00", "retry_at": None, "release_title": "Hot Fuss"}
    urls = {"self": f"{BASE}/html", "ensure": f"{BASE}/info/ensure", "status": f"{BASE}/info/status",
            "refresh_info": f"{BASE}/info/refresh", "reject_info": f"{BASE}/info/reject",
            "pin_info": f"{BASE}/info/pin", "download_missing": f"{BASE}/download-missing", "cover": f"{BASE}/cover"}
    context = {"key": KEY, "provider": "library", "album_id": KEY, "album": album, "tracks": tracks,
               "multi_disc": False, "info": info, "downloads_allowed": True, "bulk_download_allowed": True,
               "urls": urls, "upgrade_track_ids": [11, 12, 13], "upgrade_possible": True}
    for name, value in overrides.items():
        if name in ("album", "info", "urls"):
            context[name] = {**context[name], **value}
        else:
            context[name] = value
    return context


def render(**overrides) -> str:
    return env.get_template("partials/album_view.html").render(**_context(**overrides))


def _rows(markup: str) -> dict[str, str]:
    """Each track row's markup by its title."""
    rows = re.findall(r'<div class="track-row album-row.*?(?=<div class="track-row album-row|</div>\s*</section>)',
                      markup, re.S)
    return {html_lib.unescape(re.search(r'<div class="track-title">(.*?)</div>', r).group(1)): r for r in rows}


def _root_tag(markup: str) -> str:
    return re.search(r"<section [^>]*>", markup).group(0)


def test_root_carries_the_key_and_no_htmx_attributes():
    root = _root_tag(render())

    assert f'id="album-view-{KEY}"' in root and f'data-album-key="{KEY}"' in root
    assert "hx-" not in root


@pytest.mark.parametrize(("source", "label"), [("musicbrainz", "MusicBrainz"), ("deezer", "Deezer")])
def test_credit_names_the_source_and_links_it(source, label):
    url = "https://www.deezer.com/album/302127" if source == "deezer" else "https://musicbrainz.org/release/x"

    markup = render(info={"source": source, "source_label": label, "source_url": url})

    assert (f'<a href="{url}" target="_blank" rel="noopener noreferrer">Album info from {label}</a>'
            in markup)


def test_no_credit_without_a_found_match():
    assert "Album info from" not in render(info={"status": "not_found"})


@pytest.mark.parametrize(("info", "text"), [
    ({"pending": True}, "Looking up album info…"),
    ({"status": "not_found"}, "No album info found online; showing your library's songs."),
    ({"status": "error"}, "Album info is unavailable right now."),
    ({"status": "busy"}, "Lookups are busy, try again in a minute."),
    ({"mismatch": True}, "Album info may not match your songs."),
    ({"status": "found"}, ""),
])
def test_status_slot_says_what_the_lookup_is_doing(info, text):
    slot = re.search(r'<span class="album-info-status"[^>]*>(.*?)</span>', render(info=info), re.S).group(1)

    assert slot == text


@pytest.mark.parametrize("status", ["found", "not_found", "error", "none", "disabled", "busy", "never"])
def test_renders_for_every_info_status(status):
    assert "Hot Fuss" in render(info={"status": status})


def test_missing_row_has_one_download_button_posting_its_url():
    row = _rows(render())["Smile Like You Mean It"]

    assert row.count("dl-btn") == 1 and f'hx-post="{BASE}/download/1/3"' in row
    assert 'hx-target="this"' in row and "Not in your library" in row


def test_download_buttons_send_only_the_format_not_the_pin_link():
    markup = render()

    includes = re.findall(r'hx-include="([^"]*)"', markup)
    assert includes == [f"#dl-format-album-{KEY}"] * 3
    assert f'<select name="format" id="dl-format-album-{KEY}"' in markup


def _badge(**context) -> str:
    return env.get_template("partials/download_badge.html").render(**context)


def test_queued_missing_row_says_queued_and_an_error_leaves_its_note():
    row = _rows(render())["Smile Like You Mean It"]

    hook = html_lib.unescape(re.search(r'hx-on::after-swap="([^"]*)"', row).group(1))
    assert "event.target.matches('.badge-downloading')" in hook
    assert "this.querySelector('.album-row-missing').textContent = 'Queued'" in hook
    assert '<small class="album-row-note album-row-missing">Not in your library</small>' in row
    assert "badge-downloading" in _badge(status="queued") and "badge-downloading" in _badge(status="already_queued")
    assert "badge-downloading" not in _badge(status="error", message="Album info is not confirmed for this album.")


@pytest.mark.parametrize("title", ["Mr. Brightside", "Somebody Told Me", "Andy, You're a Star"])
def test_only_missing_rows_listen_for_a_queued_badge(title):
    assert "hx-on" not in _rows(render())[title]


def test_missing_row_without_a_download_url_offers_nothing():
    tracks = [_row("Smile Like You Mean It", "missing", 3)]

    markup = render(tracks=tracks, downloads_allowed=False, bulk_download_allowed=False)

    assert "dl-btn" not in markup and "Not in your library" in markup


@pytest.mark.parametrize("title", ["Mr. Brightside", "Somebody Told Me", "All These Things That I've Done"])
def test_songs_you_have_or_queued_get_no_download_button(title):
    row = _rows(render())[title]

    assert "/download/" not in row and ">Download<" not in row


def test_local_row_shows_the_quality_readout():
    row = _rows(render())["Mr. Brightside"]

    assert "AAC 256" in row and "Downloaded" in row


def test_local_row_offers_undo_flac_while_revertible():
    tracks = [_local("Mr. Brightside", 2, 12, db={"id": 12, "quality": "flac_16_44", "format": "flac",
                                                    "is_lossless": True, "upgrade_revertible": True})]

    assert 'hx-post="/api/qobuz/upgrade/revert/12"' in render(tracks=tracks)


def test_error_row_offers_retry():
    row = _rows(render())["Andy, You're a Star"]

    assert "Error" in row and ">Retry<" in row and f'hx-post="{BASE}/download/1/6"' in row


def test_elsewhere_row_opens_that_album_in_the_same_modal():
    row = _rows(render())["Somebody Told Me"]

    assert 'hx-get="/api/albums/a-ffffffffffffffffffff/html"' in row
    assert 'hx-target="closest .modal-body"' in row and "Also in your library on Direct Hits" in row


def test_elsewhere_loose_song_says_so():
    tracks = [_row("Somebody Told Me", "elsewhere", 4, elsewhere={"album": "Loose songs", "key": "loose"})]

    row = _rows(render(tracks=tracks))["Somebody Told Me"]

    assert 'hx-get="/api/albums/loose/html"' in row and "Also in your library as a loose song" in row


def test_bonus_song_is_marked_and_only_it():
    rows = _rows(render())

    assert "Bonus — not on this release" in rows["Glamorous Indie Rock & Roll"]
    assert "Bonus" not in rows["Mr. Brightside"]


def test_unknown_release_marks_no_bonus_and_draws_no_meter():
    tracks = [_local("Mr. Brightside", 2, 12, on_release=None)]

    markup = render(tracks=tracks, info={"status": "not_found"})

    assert "Bonus" not in markup and "album-own-meter" not in markup


def test_meter_has_one_cell_per_release_track():
    meter = re.search(r'<div class="album-own-meter".*?</div>', render(), re.S).group(0)

    assert meter.count("<span") == 6 and meter.count('class="is-local"') == 2
    assert 'aria-label="3 of 11 songs in your library"' in meter


def test_hero_counts_local_songs_against_the_release():
    assert "3 of 11 in your library" in render()


def test_hero_lists_release_facts():
    facts = re.search(r'<ul class="album-facts">(.*?)</ul>', render(), re.S).group(1)

    assert re.findall(r"<li>(.*?)</li>", facts) == ["2004-06-07", "Island", "11 tracks", "46 min"]


def test_disc_headers_only_on_multi_disc_albums():
    tracks = [_local("A", 1, 1), _local("B", 1, 2, disc_number=2)]

    assert "Disc 2" in render(tracks=tracks, multi_disc=True, album={"total_discs": 2})
    assert "album-disc-head" not in render(tracks=tracks)


def test_download_missing_asks_first_and_names_the_count():
    markup = render()

    button = re.search(r'<button[^>]*hx-post="[^"]*/download-missing"[^>]*>', markup).group(0)
    assert "hx-confirm=\"Download 1 song into 'Hot Fuss'?\"" in button
    assert 'hx-target="closest .album-view"' in button and 'hx-swap="outerHTML"' in button


def test_download_missing_hidden_unless_allowed():
    assert "download-missing" not in render(bulk_download_allowed=False)


def test_format_selector_is_scoped_to_the_album():
    assert f'id="dl-format-album-{KEY}"' in render()


def test_info_controls_post_and_replace_the_view():
    markup = render()

    for action in ("refresh", "reject", "pin"):
        tag = re.search(rf'<(?:button|form)[^>]*hx-post="{BASE}/info/{action}"[^>]*>', markup).group(0)
        assert 'hx-target="closest .album-view"' in tag and 'hx-swap="outerHTML"' in tag
    assert 'name="url"' in markup and 'hx-confirm="Forget this match and look again?"' in markup


def test_not_this_album_only_for_a_found_match():
    assert "/info/reject" not in render(info={"status": "not_found"})


@pytest.mark.parametrize("overrides", [
    {"info": {"can_enrich": False}},
    {"key": "loose", "album": {"kind": "loose", "name": "Loose songs"}, "info": {"can_enrich": False}},
])
def test_no_info_controls_when_the_album_cannot_be_looked_up(overrides):
    markup = render(**overrides)

    assert "/info/" not in markup and "album-info-status" not in markup and "Album info from" not in markup


def test_loose_view_counts_songs_without_an_album():
    markup = render(key="loose", album={"kind": "loose", "name": "Loose songs", "local_count": 45, "total_tracks": None},
                    info={"can_enrich": False}, tracks=[_local("Song", None, 1, on_release=None)])

    assert "45 songs without an album" in markup and 'id="album-view-loose"' in markup


def test_empty_tracklist_renders_a_note():
    assert "No songs to show for this album." in render(tracks=[])


def test_ensure_fires_once_only_when_due():
    ensure = f'hx-post="{BASE}/info/ensure" hx-trigger="load" hx-swap="none"'

    assert ensure in render(info={"should_ensure": True})
    assert "/info/ensure" not in render()


def test_poller_targets_the_status_slot_while_pending():
    markup = render(info={"pending": True})

    poller = re.search(r'<div hidden hx-get="[^"]*/info/status"[^>]*>', markup).group(0)
    assert 'hx-trigger="every 2s"' in poller and f'hx-target="#album-view-{KEY} .album-info-status"' in poller
    assert "/info/status" not in render()


def test_view_refreshes_itself_when_its_info_is_ready():
    listener = re.search(rf'<div hidden hx-get="{BASE}/html"[^>]*>', render()).group(0)

    assert html_lib.unescape(listener).count(f"album-info:ready[detail.key=='{KEY}'] from:body") == 1
    assert "qobuz:queued" in listener and f'hx-select="#album-view-{KEY}"' in listener


def test_upgrade_slot_lists_the_done_rows():
    slot = re.search(r'<div id="qobuz-upgrade-album-slot"[^>]*>', render()).group(0)

    assert '"track_ids": "11,12,13"' in html_lib.unescape(slot)
    assert "qobuz-upgrade-album-slot" not in render(upgrade_possible=False)


def test_missing_cover_shows_the_placeholder():
    markup = render(album={"image_url": None})

    assert 'src="/static/img/placeholder.svg"' in markup and "--hero-bg-url" not in markup


def test_cover_falls_back_when_it_fails_to_load():
    img = re.search(r'<img [^>]*class="album-art"[^>]*>', render()).group(0)

    assert f'src="{BASE}/cover?t=11"' in img and "data-cover-fallback" in img


def test_notice_and_spotify_retry():
    markup = render(notice="Spotify is rate-limiting Leasure <now>",
                    retry_url="/api/music/album/spotify/abc?name=Hot+Fuss&artist=The+Killers")

    assert '<p class="detail-notice" role="status">Spotify is rate-limiting Leasure &lt;now&gt;</p>' in markup
    assert "Try Spotify again" in markup


def test_provider_text_is_escaped():
    markup = render(album={"name": "<script>x</script>"})

    assert "<script>x</script>" not in markup and "&lt;script&gt;" in markup


def test_minimal_context_renders():
    markup = env.get_template("partials/album_view.html").render(
        key=KEY, album={"key": KEY, "kind": "album", "name": "Hot Fuss", "artist": "The Killers", "total_discs": 1},
        tracks=[], provider="library", album_id=KEY, multi_disc=False)

    assert "Hot Fuss" in markup and "dl-btn" not in markup
