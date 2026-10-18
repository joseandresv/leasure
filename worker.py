import asyncio
import itertools
import logging
import time

from sqlalchemy import select, update

from config import settings
from db import async_session
from models import Track, utc_now
from services import qobuz_client, qobuz_engine
from services.yt_engine import CapDeferred, DownloadFailed
from services.ytdlp_opts import CookieSessionRefused

logger = logging.getLogger(__name__)

SCHEDULER_TICK_S = 30
DOWNLOAD_PRIORITY, UPGRADE_PRIORITY = 0, 1


class DownloadWorker:
    def __init__(self, max_concurrent: int = settings.max_concurrent_downloads):
        # (priority, seq, track_id): new downloads go ahead of FLAC upgrades.
        self.queue: asyncio.PriorityQueue[tuple[int, int, int]] = asyncio.PriorityQueue()
        self.max_concurrent = max_concurrent
        self._tasks: list[asyncio.Task] = []
        self._seq = itertools.count()
        self._queued: dict[int, int] = {}
        # Held back by a cap, cooldown or breaker: track_id -> (due time.time(), priority).
        self._deferred: dict[int, tuple[float, int]] = {}

    async def start(self):
        await qobuz_engine.recover_upgrade_journals()
        await asyncio.to_thread(qobuz_engine.purge_old_backups)
        if not settings.download_worker_enabled:
            logger.warning("Download worker disabled (DOWNLOAD_WORKER_ENABLED=false): queued tracks stay pending")
            return
        # Recover orphaned tracks from previous crash/restart
        await self._recover_orphaned()

        self._tasks = [
            asyncio.create_task(self._worker(i))
            for i in range(self.max_concurrent)
        ]
        self._tasks.append(asyncio.create_task(self._scheduler()))
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
            await session.execute(update(Track).where(Track.upgrade_state == "downloading")
                                  .values(upgrade_state="queued").execution_options(synchronize_session=False))
            await session.commit()

            # Re-enqueue all pending tracks
            result = await session.execute(
                select(Track).where(Track.status == "pending").order_by(Track.id)
            )
            pending = result.scalars().all()
            if pending:
                logger.info("Re-queuing %d orphaned tracks", len(pending))
                for track in pending:
                    await self.enqueue(track.id)
        await self.top_up_upgrades()

    async def stop(self):
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        logger.info("Download worker stopped")

    async def enqueue(self, track_id: int, *, upgrade: bool = False):
        """Queue a track once; a deferred track (manual Retry) goes back in at once."""
        if track_id in self._queued:
            return
        self._deferred.pop(track_id, None)
        priority = UPGRADE_PRIORITY if upgrade else DOWNLOAD_PRIORITY
        self._queued[track_id] = priority
        self.queue.put_nowait((priority, next(self._seq), track_id))
        logger.info("Enqueued track %d for %s", track_id, "a FLAC upgrade" if upgrade else "download")

    @property
    def queue_size(self) -> int:
        return self.queue.qsize()

    async def _scheduler(self):
        while True:
            await asyncio.sleep(SCHEDULER_TICK_S)
            try:
                await self._tick()
            except Exception:
                logger.exception("Download scheduler tick failed")

    async def _tick(self):
        now = time.time()
        for track_id, (due, priority) in list(self._deferred.items()):
            if due <= now:
                await self.enqueue(track_id, upgrade=priority == UPGRADE_PRIORITY)
        await self.top_up_upgrades()

    async def top_up_upgrades(self):
        """Feed queued FLAC upgrades, only while no download waits and only what today's Qobuz cap allows."""
        if any(p == DOWNLOAD_PRIORITY for p in self._queued.values()) or not await qobuz_engine.gate_open():
            return
        cap_left = settings.qobuz_max_downloads_per_day - await asyncio.to_thread(qobuz_engine.downloads_today)
        room = cap_left - sum(1 for p in self._queued.values() if p == UPGRADE_PRIORITY)
        if room <= 0:
            return
        busy = set(self._queued) | set(self._deferred)
        async with async_session() as session:
            ids = (await session.scalars(select(Track.id).where(Track.status == "done", Track.upgrade_state == "queued")
                                         .order_by(Track.id))).all()
        for track_id in [i for i in ids if i not in busy][:room]:
            await self.enqueue(track_id, upgrade=True)

    async def _worker(self, worker_id: int):
        while True:
            _, _, track_id = await self.queue.get()
            self._queued.pop(track_id, None)
            try:
                await self._process_track(worker_id, track_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Worker %d failed on track %d: %s", worker_id, track_id, e)
                try:
                    await self._set_status(track_id, "error", qobuz_client.redact(str(e)) or e.__class__.__name__)
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
                if track.upgrade_state == "queued":
                    await self._process_upgrade(track_id)
                else:
                    logger.info("Track %d already downloaded, skipping", track_id)
                return

        await self._set_status(track_id, "downloading")

        # Import here to avoid circular imports and allow lazy loading
        from services.downloader import download_track

        try:
            result_path = await download_track(track_id)
        except CapDeferred as e:
            # Not a failure of this track: it stays pending and the scheduler brings it back.
            await self._set_status(track_id, "pending", str(e))
            self._deferred[track_id] = (time.time() + e.retry_after_s, DOWNLOAD_PRIORITY)
            logger.info("Track %d deferred for %.0f s: %s", track_id, e.retry_after_s, e)
            return
        except (DownloadFailed, CookieSessionRefused) as e:
            # An engine that explains itself has already said everything the user needs;
            # a stack trace would only bury the message.
            logger.warning("Track %d failed: %s", track_id, e)
            await self._set_status(track_id, "error", qobuz_client.redact(str(e)))
            return

        async with async_session() as session:
            track = await session.get(Track, track_id)
            track.file_path = str(result_path)
            track.status = "done"
            track.downloaded_at = utc_now()
            await session.commit()
        logger.info("Track %d downloaded to %s", track_id, result_path)

    async def _process_upgrade(self, track_id: int):
        await self._set_upgrade(track_id, "downloading", None)
        try:
            path = await qobuz_engine.qobuz_upgrade(track_id)
        except CapDeferred as e:
            await self._set_upgrade(track_id, "queued", str(e))
            self._deferred[track_id] = (time.time() + e.retry_after_s, UPGRADE_PRIORITY)
            return
        except qobuz_engine.QobuzSkip as e:
            await self._set_upgrade(track_id, "error", str(e), skip_reason=e.reason)
            return
        except DownloadFailed as e:
            await self._set_upgrade(track_id, "error", qobuz_client.redact(str(e)))
            return
        except Exception as e:
            # The track itself is still done: record the upgrade failure on it, never status=error.
            logger.exception("FLAC upgrade of track %d failed", track_id)
            await self._set_upgrade(track_id, "error", f"Upgrade failed ({e.__class__.__name__}).")
            return
        logger.info("Track %d upgraded to %s", track_id, path)

    async def _set_upgrade(self, track_id: int, state: str, error: str | None, *, skip_reason: str | None = None):
        values = {"upgrade_state": state, "upgrade_error": error}
        if skip_reason:
            values["qobuz_skip_reason"] = skip_reason
        async with async_session() as session:
            await session.execute(update(Track).where(Track.id == track_id).values(**values)
                                  .execution_options(synchronize_session=False))
            await session.commit()

    async def _set_status(self, track_id: int, status: str, error: str | None = None):
        async with async_session() as session:
            track = await session.get(Track, track_id)
            if track:
                track.status = status
                track.error_message = error
                await session.commit()


# Singleton instance
download_worker = DownloadWorker()
