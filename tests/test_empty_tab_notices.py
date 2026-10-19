import pytest

from routers.music import templates

NOTICE = "Spotify is rate-limiting Leasure (try again in ~2 h)"

TABS = [
    ("partials/rhythm_deck.html", "tracks",
     f"No recent listening to show. {NOTICE}; YouTube Music history still shows here.",
     "No recent listening to spin. Play something on Spotify or YT Music."),
    ("partials/music_playlists.html", "playlists",
     f"No playlists found. {NOTICE}; YouTube Music playlists still show here.",
     "No playlists found."),
    ("partials/music_artists.html", "artists",
     f"No artists found. {NOTICE}; YouTube Music artists still show here.",
     "No artists found."),
]


def render(name: str, **context) -> str:
    return templates.env.get_template(name).render(**context)


@pytest.mark.parametrize(("name", "key", "with_notice", "_"), TABS)
def test_empty_tab_says_spotify_is_rate_limiting(name, key, with_notice, _):
    html = render(name, **{key: [], "spotify_notice": NOTICE})

    assert f'<p class="deck-empty">{with_notice}</p>' in html


@pytest.mark.parametrize(("name", "key", "_", "without_notice"), TABS)
def test_empty_tab_keeps_its_text_without_a_notice(name, key, _, without_notice):
    html = render(name, **{key: [], "spotify_notice": None})

    assert f'<p class="deck-empty">{without_notice}</p>' in html and "rate-limiting" not in html
