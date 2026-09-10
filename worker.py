import asyncio
import logging

from sqlalchemy import select

from config import settings
from db import async_session
from models import Track, utc_now
from services import yt_engine
from services.ytdlp_opts import CookieSessionRefused

logger = logging.getLogger(__name__)

# How long a worker waits before looking at a capped track again.
CAP_RETRY_SECONDS = 300


class DownloadWorker:
    def __init__(self, max_concurrent: int = settings.max_concurrent_downloads):
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self.max_concurrent = max_concurrent
        self._tasks: list[asyncio.Task] = []

    async def start(self):
        # Recover orphaned tracks from previous crash/restart
        await self._recover_orphaned()

        self._tasks = [
            asyncio.create_task(self._worker(i))
            for i in range(self.max_concurrent)
        ]
        logger.info("Download worker started with %d concurrent slots", self.max_concurrent)

    async def _recover_orphaned(self):
        """Re-queue tracks stuck in 'downloading' or 'pending' from a previous run."""
        async with async_session() as session:
            # Reset 'downloading' back to 'pending' (interrupted mid-download)
            result = await session.execute(
                select(Track).where(Track.status == "downloading")
            )
            for track in result.scalars().all():
                track.status = "pending"
                logger.info("Reset orphaned track %d (%s) from downloading to pending", track.id, track.title)
            await session.commit()

            # Re-enqueue all pending tracks
            result = await session.execute(
                select(Track).where(Track.status == "pending")
            )
            pending = result.scalars().all()
            if pending:
                logger.info("Re-queuing %d orphaned tracks", len(pending))
                for track in pending:
                    await self.queue.put(track.id)

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Download worker stopped")

    async def enqueue(self, track_id: int):
        await self.queue.put(track_id)
        logger.info("Enqueued track %d for download", track_id)

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()

    async def _worker(self, worker_id: int):
        while True:
            track_id = await self.queue.get()
            try:
                await self._process_track(worker_id, track_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Worker %d failed on track %d: %s", worker_id, track_id, e)
                try:
                    await self._set_status(track_id, "error", str(e))
                except Exception:
                    # The consumer loop must survive a DB hiccup here, or the
                    # queue silently stops being drained.
                    logger.exception("Worker %d could not record error for track %d", worker_id, track_id)
            finally:
                self.queue.task_done()

    async def _process_track(self, worker_id: int, track_id: int):
        logger.info("Worker %d processing track %d", worker_id, track_id)

        async with async_session() as session:
            track = await session.get(Track, track_id)
            if not track:
                logger.warning("Track %d not found", track_id)
                return
            if track.status == "done":
                logger.info("Track %d already downloaded, skipping", track_id)
                return

        if await asyncio.to_thread(yt_engine.daily_cap_reached):
            # Stay pending and go back in the queue: the cap protects the owner's
            # Google session, it is not a failure of this track.
            await self._set_status(
                track_id, "pending",
                f"Daily YouTube download cap reached ({yt_engine.MAX_DOWNLOADS_PER_DAY} today) — "
                "this track stays queued and resumes tomorrow.")
            await self.queue.put(track_id)
            logger.info("Track %d deferred: daily download cap reached", track_id)
            await asyncio.sleep(CAP_RETRY_SECONDS)
            return

        await self._set_status(track_id, "downloading")

        # Import here to avoid circular imports and allow lazy loading
        from services.downloader import download_track

        try:
            result_path = await download_track(track_id)
        except (yt_engine.DownloadFailed, CookieSessionRefused) as e:
            # An engine that explains itself has already said everything the user needs;
            # a stack trace would only bury the message.
            logger.warning("Track %d failed: %s", track_id, e)
            await self._set_status(track_id, "error", str(e))
            return
        except Exception as e:
            await self._set_status(track_id, "error", str(e) or e.__class__.__name__)
            raise

        async with async_session() as session:
            track = await session.get(Track, track_id)
            track.file_path = str(result_path)
            track.status = "done"
            track.downloaded_at = utc_now()
            await session.commit()
        logger.info("Track %d downloaded to %s", track_id, result_path)

    async def _set_status(self, track_id: int, status: str, error: str | None = None):
        async with async_session() as session:
            track = await session.get(Track, track_id)
            if track:
                track.status = status
                track.error_message = error
                await session.commit()


# Singleton instance
download_worker = DownloadWorker()
