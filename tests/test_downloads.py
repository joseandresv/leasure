import os
import time
from datetime import datetime

import pytest

from routers.downloads import _format_downloaded_at


@pytest.fixture
def madrid_timezone():
    previous = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Madrid"
    time.tzset()
    yield
    if previous is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = previous
    time.tzset()


def test_downloaded_at_renders_in_local_time(madrid_timezone):
    assert _format_downloaded_at(datetime(2026, 9, 10, 17, 9)) == "10 Sep 2026, 19:09"


def test_downloaded_at_of_never_downloaded_track_is_none():
    assert _format_downloaded_at(None) is None
