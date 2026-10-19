"""One lock around "is this song already in the library or queued? then queue it", shared by every bulk
queueing path (album view, Saved downloads, the Download all feeder), so two of them never add the same song twice."""

import asyncio

DOWNLOAD_LOCK = asyncio.Lock()
