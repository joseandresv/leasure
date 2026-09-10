import re

import pytest

from models import Track

HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


async def _seed(db, rows):
    async with db() as session:
        for i, (album, genre) in enumerate(rows):
            session.add(Track(title=f"Track {i}", artist="A", album=album,
                              album_artist="A", genre=genre, status="done"))
        await session.commit()


@pytest.mark.asyncio
async def test_graph_counts_genres_per_track_not_per_album(db, client):
    await _seed(db, [
        ("Neon Album", "synthwave"),
        ("Neon Album", "synthwave"),
        ("Other Album", "vaporwave"),
        ("Other Album", "other"),
    ])

    genres = (await client.get("/api/library/graph")).json()["genres"]

    assert {g: info["count"] for g, info in genres.items()} == {
        "synthwave": 2, "vaporwave": 1, "other": 1,
    }
    assert all(HEX_COLOR.match(info["color"]) for info in genres.values())


@pytest.mark.asyncio
async def test_graph_node_leads_with_the_albums_dominant_genre(db, client):
    await _seed(db, [
        ("Neon Album", "synthwave, retrowave"),
        ("Neon Album", "synthwave"),
    ])

    data = (await client.get("/api/library/graph")).json()

    assert data["nodes"][0]["genres"] == ["synthwave", "retrowave"]
    assert data["genres"]["retrowave"]["count"] == 1


@pytest.mark.asyncio
async def test_graph_keeps_same_titled_albums_of_different_artists_apart(db, client):
    async with db() as session:
        session.add(Track(title="t1", artist="Aphex Twin", album="Greatest Hits",
                          album_artist="Aphex Twin", genre="idm", status="done"))
        session.add(Track(title="t2", artist="Queen", album="Greatest Hits",
                          album_artist="Queen", genre="rock", status="done"))
        await session.commit()

    nodes = (await client.get("/api/library/graph")).json()["nodes"]

    assert sorted(n["artist"] for n in nodes) == ["Aphex Twin", "Queen"]
