"""Core synchronization engine implementing icloudpd-parity workflows."""

from __future__ import annotations

from concurrent.futures import CancelledError, Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
import fnmatch
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo

from hwcloudpd.client import SYSTEM_ALBUM_NAMES, VIRTUAL_ALBUM_TYPE_MAP, HuaweiGalleryClient
from hwcloudpd.exif import inject_exif_datetime_if_missing
from hwcloudpd.fileutil import checksum_matches, hash_file
from hwcloudpd.models import (
    AlbumInfo,
    AuthExpiredError,
    HwCloudError,
    MediaItem,
    MediaStub,
    SyncCancelledError,
    SyncConfig,
    SyncReport,
)
from hwcloudpd.postprocess import (
    TargetDecision,
    extract_motion_photo_video,
    resolve_target,
    sanitize_component,
    write_xmp_sidecar,
)
from hwcloudpd.store import (
    CLOUD_DELETE_FAILED,
    CLOUD_DELETE_PENDING,
    CLOUD_DELETED,
    CLOUD_MISSING,
    CLOUD_PRESENT,
    STATUS_DOWNLOADED,
    StateStore,
)

logger = logging.getLogger(__name__)

RECYCLE_ALBUM_ID = "default-album-3"
PART_DIR_NAME = ".hwcloudpd-tmp"
STALE_PART_MAX_AGE_SEC = 7 * 86400


def _matches_any_pattern(album: AlbumInfo, patterns: list[str]) -> bool:
    for pat in patterns:
        p = pat.strip()
        if not p:
            continue
        if p == album.album_id or p == album.name:
            return True
        if fnmatch.fnmatch(album.name, p) or fnmatch.fnmatch(album.album_id, p):
            return True
    return False


def motion_video_path(jpg_path: Path, policy: str) -> Path:
    """Return the path an extracted Motion Photo video would be written to."""
    if policy == "original":
        return jpg_path.with_suffix(".mp4")
    return jpg_path.with_name(f"{jpg_path.stem}_HEVC.mp4")


@dataclass
class _Source:
    album_id: str | None
    album_name: str
    album_version: str = ""
    owner_id: str | None = None
    is_shared: bool = False
    # When set, only items whose albumId is in this set are considered (used by --recent).
    allowed_album_ids: set[str] | None = None


@dataclass
class _RunContext:
    report: SyncReport
    is_dry: bool
    full_scan: bool
    effective_until_found: int | None
    excluded_ids: set[str]
    name_by_id: dict[str, str]
    album_by_id: dict[str, AlbumInfo]
    recent_remaining: int | None
    seen_uids: set[str] = field(default_factory=set)
    visible_uids: set[str] = field(default_factory=set)
    pending: list[MediaStub] = field(default_factory=list)
    verify: list[MediaStub] = field(default_factory=list)
    replace_uids: set[str] = field(default_factory=set)
    # Items downloaded / adopted / re-downloaded in *this* run (delete-after-download scope).
    backed_up_uids: set[str] = field(default_factory=set)
    reserved: set[Path] = field(default_factory=set)
    path_lock: threading.Lock = field(default_factory=threading.Lock)
    preprocessed: bool = False
    pool: ThreadPoolExecutor | None = None


class SyncEngine:
    """Orchestrates album discovery, incremental filtering, atomic download, and sync/move modes."""

    BATCH_SIZE = 200
    VERIFY_BATCH_SIZE = 500

    def __init__(
        self,
        config: SyncConfig,
        client: HuaweiGalleryClient,
        store: StateStore,
        *,
        output_fn: Callable[[str], None] = print,
    ) -> None:
        self.config = config
        self.client = client
        self.store = store
        self.output_fn = output_fn
        self._tz = ZoneInfo(config.timezone)
        self._stop = threading.Event()
        self._abort = threading.Event()

    # ------------------------------------------------------------------ control

    def request_stop(self) -> None:
        """Ask a running `run_once` to stop after the files currently in flight."""
        self._stop.set()

    def _check_stop(self) -> None:
        if self._stop.is_set():
            raise SyncCancelledError("Sync stop requested")

    # ------------------------------------------------------------------ selection

    def _select_albums(
        self, all_albums: list[AlbumInfo]
    ) -> tuple[list[AlbumInfo], set[str], dict[str, str]]:
        """Select target albums (default: all non-recycle albums) plus exclusion metadata."""
        name_by_id = {a.album_id: a.name for a in all_albums}
        name_by_id.update(SYSTEM_ALBUM_NAMES)

        excluded_ids: set[str] = {RECYCLE_ALBUM_ID}
        for album in all_albums:
            if album.is_recycle or _matches_any_pattern(album, self.config.exclude_albums):
                excluded_ids.add(album.album_id)

        if self.config.albums:
            by_id = {a.album_id: a for a in all_albums}
            selected: list[AlbumInfo] = []
            selected_ids: set[str] = set()

            # Allow selecting virtual views explicitly even though they are not in normal album list.
            virtual_candidates = [
                AlbumInfo(
                    album_id=vid,
                    name=SYSTEM_ALBUM_NAMES.get(vid, vid),
                    is_virtual=True,
                    is_recycle=(vid == RECYCLE_ALBUM_ID),
                )
                for vid in VIRTUAL_ALBUM_TYPE_MAP
                if vid != RECYCLE_ALBUM_ID
            ]
            pool = [a for a in all_albums if not a.is_recycle] + virtual_candidates
            for req in self.config.albums:
                req_s = req.strip()
                if not req_s:
                    continue
                for candidate in pool:
                    if candidate.album_id in excluded_ids or candidate.album_id in selected_ids:
                        continue
                    if _matches_any_pattern(candidate, [req_s]):
                        selected.append(candidate)
                        selected_ids.add(candidate.album_id)
                if req_s in by_id and req_s not in excluded_ids and req_s not in selected_ids:
                    selected.append(by_id[req_s])
                    selected_ids.add(req_s)
            return selected, excluded_ids, name_by_id

        selected = [
            a
            for a in all_albums
            if not a.is_recycle and not a.is_virtual and a.album_id not in excluded_ids
        ]
        return selected, excluded_ids, name_by_id

    def _build_sources(self, target_albums: list[AlbumInfo]) -> list[_Source]:
        timeline = _Source(album_id=None, album_name="")
        if self.config.recent is not None and not any(a.is_virtual for a in target_albums):
            # --recent N means the N most recent items overall, so walk the global timeline
            # (sorted by date) and restrict it to the selected albums.
            if self.config.albums:
                timeline.allowed_album_ids = {a.album_id for a in target_albums}
            return [timeline]
        sources = [
            _Source(
                album_id=a.album_id,
                album_name=a.name,
                album_version=a.album_version,
                owner_id=a.owner_id,
                is_shared=a.is_shared,
            )
            for a in target_albums
        ]
        if not self.config.albums:
            # In default all-album mode, also sweep the global timeline so items not assigned to
            # any queryAlbumInfo album are never missed.
            sources.append(timeline)
        return sources

    # ------------------------------------------------------------------ filters

    def _passes_stub_filters(self, stub: MediaStub) -> bool:
        if self.config.skip_photos and stub.file_type not in ("4", "5"):
            return False
        if self.config.skip_videos and stub.file_type == "4":
            return False
        if self.config.skip_live_photos and stub.file_type == "5":
            return False
        if self.config.only_favorites and stub.is_fav != 1:
            return False
        if (
            self.config.created_after_ms is not None
            and stub.create_time_ms > 0
            and stub.create_time_ms < self.config.created_after_ms
        ):
            return False
        if (
            self.config.created_before_ms is not None
            and stub.create_time_ms > 0
            and stub.create_time_ms > self.config.created_before_ms
        ):
            return False
        return True

    def _passes_size_filters(self, item: MediaItem) -> bool:
        if item.file_size <= 0:
            return True
        if self.config.min_size is not None and item.file_size < self.config.min_size:
            return False
        if self.config.max_size is not None and item.file_size > self.config.max_size:
            return False
        return True

    def _synced_record(self, unique_id: str) -> dict[str, Any] | None:
        rec = self.store.get_record(unique_id)
        if rec is None or rec["status"] != STATUS_DOWNLOADED:
            return None
        if rec["size_variant"] != self.config.photo_size or not rec["local_rel_path"]:
            return None
        if not (self.config.directory / rec["local_rel_path"]).is_file():
            return None
        return rec

    # ------------------------------------------------------------------ bursts

    @staticmethod
    def _stub_to_cache(stub: MediaStub) -> dict[str, Any]:
        return {
            "unique_id": stub.unique_id,
            "album_id": stub.album_id,
            "file_type": stub.file_type,
            "create_time_ms": stub.create_time_ms,
            "is_fav": stub.is_fav,
            "file_name": stub.file_name,
        }

    @staticmethod
    def _stub_from_cache(cover: MediaStub, data: dict[str, Any]) -> MediaStub:
        return MediaStub(
            unique_id=str(data.get("unique_id") or ""),
            album_id=str(data.get("album_id") or cover.album_id),
            album_name=cover.album_name,
            file_type=str(data.get("file_type") or cover.file_type),
            create_time_ms=int(data.get("create_time_ms") or cover.create_time_ms),
            is_cover=1 if data.get("unique_id") == cover.unique_id else 0,
            is_fav=int(data.get("is_fav") or 0),
            file_name=str(data.get("file_name") or ""),
            album_version=cover.album_version,
            owner_id=cover.owner_id,
            is_shared=cover.is_shared,
        )

    def _expand_burst_if_needed(self, stub: MediaStub) -> list[MediaStub]:
        if stub.is_cover != 1 or self.config.skip_bursts:
            return [stub]

        cached_members: list[MediaStub] = []
        cached = self.store.get_burst_members(stub.unique_id)
        if cached:
            cached_members = [self._stub_from_cache(stub, m) for m in cached if m.get("unique_id")]
            # Every frame already backed up -> no need to call burstphotolist again.
            if cached_members and all(
                self._synced_record(m.unique_id) is not None for m in cached_members
            ):
                return cached_members

        try:
            bursts = self.client.get_burst_files(
                stub.album_id,
                stub.unique_id,
                album_name=stub.album_name,
                album_version=stub.album_version,
                owner_id=stub.owner_id,
            )
            if bursts:
                if not any(b.unique_id == stub.unique_id for b in bursts):
                    bursts.insert(0, stub)
                for b in bursts:
                    b.is_shared = stub.is_shared
                    b.album_name = stub.album_name
                self.store.set_burst_members(
                    stub.unique_id, [self._stub_to_cache(b) for b in bursts]
                )
                return bursts
        except AuthExpiredError:
            raise
        except HwCloudError as exc:
            logger.warning("Failed to expand burst %s: %s", stub.unique_id, exc)
        return cached_members or [stub]

    # ------------------------------------------------------------------ scanning

    def _scan_source(self, source: _Source, ctx: _RunContext) -> None:
        consecutive_found = 0
        report = ctx.report

        for page in self.client.iter_simple_files(
            source.album_id,
            album_name=source.album_name,
            album_version=source.album_version,
            owner_id=source.owner_id,
            page_size=self.config.page_size,
            request_delay_ms=self.config.request_delay_ms,
        ):
            self._check_stop()
            stop_source = False
            for raw_stub in page:
                ctx.visible_uids.add(raw_stub.unique_id)
                if raw_stub.album_id in ctx.excluded_ids:
                    continue
                if (
                    source.allowed_album_ids is not None
                    and raw_stub.album_id not in source.allowed_album_ids
                ):
                    continue
                if raw_stub.unique_id in ctx.seen_uids:
                    continue
                if raw_stub.album_id in ctx.name_by_id:
                    raw_stub.album_name = ctx.name_by_id[raw_stub.album_id]
                album = ctx.album_by_id.get(raw_stub.album_id)
                if album is not None:
                    raw_stub.album_version = raw_stub.album_version or album.album_version
                    raw_stub.owner_id = raw_stub.owner_id or album.owner_id
                    raw_stub.is_shared = raw_stub.is_shared or album.is_shared
                raw_stub.is_shared = raw_stub.is_shared or source.is_shared

                if ctx.recent_remaining is not None:
                    if ctx.recent_remaining <= 0:
                        stop_source = True
                        break
                    ctx.recent_remaining -= 1

                for stub in self._expand_burst_if_needed(raw_stub):
                    ctx.visible_uids.add(stub.unique_id)
                    if stub.album_id in ctx.excluded_ids or stub.unique_id in ctx.seen_uids:
                        continue
                    ctx.seen_uids.add(stub.unique_id)
                    report.scanned += 1

                    if not self._passes_stub_filters(stub):
                        report.skipped_filtered += 1
                        continue

                    rec = self._synced_record(stub.unique_id)
                    if rec is not None:
                        self._on_existing(stub, rec, ctx)
                        report.skipped_existing += 1
                        consecutive_found += 1
                        if (
                            ctx.effective_until_found is not None
                            and consecutive_found >= ctx.effective_until_found
                        ):
                            logger.debug(
                                "Early-stop triggered for album=%s after %d existing items",
                                source.album_id or "timeline",
                                consecutive_found,
                            )
                            stop_source = True
                            break
                        continue

                    if self.store.is_failure_deferred(stub.unique_id):
                        report.deferred_failed += 1
                        continue

                    consecutive_found = 0
                    ctx.pending.append(stub)
                    if len(ctx.pending) >= self.BATCH_SIZE:
                        self._flush_pending(ctx)

                if stop_source:
                    break
            if stop_source:
                break

    def _on_existing(self, stub: MediaStub, rec: dict[str, Any], ctx: _RunContext) -> None:
        state = rec.get("cloud_state") or CLOUD_PRESENT
        if state in (CLOUD_DELETED, CLOUD_MISSING) and not ctx.is_dry:
            # Visible in a normal album again -> the user restored it from the Recycle Bin.
            logger.info("Cloud item %s is visible again (restored); marking present", stub.unique_id)
            self.store.set_cloud_state([stub.unique_id], CLOUD_PRESENT)
        if ctx.full_scan and self.config.photo_size == "original":
            ctx.verify.append(stub)
            if len(ctx.verify) >= self.VERIFY_BATCH_SIZE:
                self._flush_verify(ctx)

    # ------------------------------------------------------------------ batches

    def _flush_pending(self, ctx: _RunContext) -> None:
        stubs, ctx.pending = ctx.pending, []
        if not stubs:
            return
        self._check_stop()
        items_to_download: list[MediaItem] = []
        for item in self.client.enrich_metadata(stubs):
            if not self._passes_size_filters(item):
                ctx.report.skipped_filtered += 1
                continue
            items_to_download.append(item)
        self._process_items(items_to_download, ctx)

    def _flush_verify(self, ctx: _RunContext) -> None:
        """Full-scan only: detect cloud items whose content changed (e.g. edited on the phone)."""
        stubs, ctx.verify = ctx.verify, []
        if not stubs:
            return
        self._check_stop()
        changed: list[MediaItem] = []
        for item in self.client.enrich_metadata(stubs):
            rec = self.store.get_record(item.unique_id)
            if rec is None or item.name_synthesized:
                continue
            old_sum = str(rec.get("checksum") or "").lower()
            new_sum = (item.checksum or "").lower()
            old_size = int(rec.get("file_size") or 0)
            if old_sum and new_sum:
                is_changed = old_sum != new_sum
            else:
                is_changed = old_size > 0 and item.file_size > 0 and old_size != item.file_size
            if is_changed:
                logger.info(
                    "Cloud content of %s (%s) changed since backup; re-downloading",
                    item.file_name,
                    item.unique_id,
                )
                ctx.replace_uids.add(item.unique_id)
                changed.append(item)
        self._process_items(changed, ctx)

    def _process_items(self, items: list[MediaItem], ctx: _RunContext) -> None:
        if not items:
            return
        if ctx.is_dry:
            self._dry_run_items(items, ctx)
            return

        if not ctx.preprocessed:
            self.client.pre_process()
            ctx.preprocessed = True

        workers = max(1, self.config.download_concurrency)
        if workers == 1:
            for item in items:
                self._check_stop()
                try:
                    outcome = self._process_item(item, ctx)
                except AuthExpiredError:
                    raise
                except SyncCancelledError:
                    raise
                except Exception as exc:
                    self._record_item_failure(item, exc, ctx)
                else:
                    self._record_item_success(item, outcome, ctx)
            return

        if ctx.pool is None:
            ctx.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="hwcloudpd-dl")
        future_map: dict[Future, MediaItem] = {
            ctx.pool.submit(self._process_item, item, ctx): item for item in items
        }
        auth_exc: AuthExpiredError | None = None
        for fut in as_completed(future_map):
            item = future_map[fut]
            try:
                outcome = fut.result()
            except AuthExpiredError as exc:
                if auth_exc is None:
                    auth_exc = exc
                    # Stop queued downloads immediately instead of letting each one hit the
                    # expired session.
                    self._abort.set()
                    for other in future_map:
                        other.cancel()
            except (SyncCancelledError, CancelledError):
                continue
            except Exception as exc:
                self._record_item_failure(item, exc, ctx)
            else:
                self._record_item_success(item, outcome, ctx)
        if auth_exc is not None:
            raise auth_exc
        self._check_stop()

    def _record_item_success(self, item: MediaItem, outcome: tuple[str, Path], ctx: _RunContext) -> None:
        kind, target = outcome
        ctx.backed_up_uids.add(item.unique_id)
        if kind == "adopted":
            ctx.report.adopted += 1
            logger.info("Adopted existing identical file %s", target)
        elif kind == "changed":
            ctx.report.redownloaded_changed += 1
            logger.info("Re-downloaded changed item %s", target)
        else:
            ctx.report.downloaded += 1
            logger.info("Downloaded %s", target)

    def _record_item_failure(self, item: MediaItem, exc: Exception, ctx: _RunContext) -> None:
        ctx.report.failed += 1
        attempts = self.store.record_failure(item.unique_id, str(exc))
        logger.error(
            "Failed to download %s (%s) [attempt %d]: %s",
            item.file_name,
            item.unique_id,
            attempts,
            exc,
        )

    def _dry_run_items(self, items: list[MediaItem], ctx: _RunContext) -> None:
        for item in items:
            dt = self._item_datetime(item)
            decision = resolve_target(
                self.config.directory,
                folder_structure=self.config.folder_structure,
                file_match_policy=self.config.file_match_policy,
                item=item,
                dt=dt,
                store=self.store,
                reserved=ctx.reserved,
                allow_adopt=self._allow_adopt(item, ctx),
            )
            ctx.reserved.add(decision.path)
            if decision.adopt:
                ctx.report.adopted += 1
                logger.info("[DRY-RUN] Would adopt existing identical file %s", decision.path)
                continue
            if self.config.only_print_filenames:
                self.output_fn(str(decision.path))
            else:
                logger.info("[DRY-RUN] Would download %s -> %s", item.file_name, decision.path)
            if item.unique_id in ctx.replace_uids:
                ctx.report.redownloaded_changed += 1
            else:
                ctx.report.downloaded += 1

    # ------------------------------------------------------------------ single item

    def _item_datetime(self, item: MediaItem) -> datetime:
        if item.create_time_ms > 0:
            return datetime.fromtimestamp(item.create_time_ms / 1000.0, tz=self._tz)
        return datetime.now(tz=self._tz)

    def _allow_adopt(self, item: MediaItem, ctx: _RunContext) -> bool:
        return (
            self.config.photo_size == "original"
            and not item.name_synthesized
            and item.unique_id not in ctx.replace_uids
        )

    def _part_path(self, item: MediaItem) -> Path:
        safe_uid = sanitize_component(item.unique_id, fallback="item")
        return self.config.directory / PART_DIR_NAME / f"{safe_uid}.{self.config.photo_size}.hwpart"

    def _rel(self, path: Path) -> str:
        return str(path.relative_to(self.config.directory))

    def _download_verified(self, item: MediaItem, part_path: Path):
        last_exc: Exception | None = None
        for attempt in range(2):
            try:
                url = self.client.get_download_url(item, self.config.photo_size)
                dl_res = self.client.download_to_part(url, part_path)
                if self.config.photo_size == "original":
                    if item.file_size > 0 and dl_res.bytes_written != item.file_size:
                        part_path.unlink(missing_ok=True)
                        raise HwCloudError(
                            f"Downloaded size mismatch for {item.file_name}: "
                            f"expected {item.file_size}, got {dl_res.bytes_written}"
                        )
                    verdict = checksum_matches(item.checksum, dl_res.sha256_hex, dl_res.md5_hex)
                    if verdict is False:
                        part_path.unlink(missing_ok=True)
                        raise HwCloudError(
                            f"Checksum mismatch for {item.file_name}: expected {item.checksum}, "
                            f"got sha256={dl_res.sha256_hex} md5={dl_res.md5_hex}"
                        )
                    if verdict is None:
                        logger.debug("No cloud checksum for %s; size-only verification", item.file_name)
                return dl_res
            except (AuthExpiredError, SyncCancelledError):
                raise
            except Exception as exc:
                last_exc = exc
                if attempt == 0 and not self._stop.is_set():
                    logger.warning("Retrying %s after error: %s", item.file_name, exc)
                    time.sleep(0.5)
                    continue
                raise
        assert last_exc is not None
        raise last_exc

    def _adopt(self, item: MediaItem, decision: TargetDecision, ctx: _RunContext) -> tuple[str, Path]:
        sha = decision.local_sha256 or hash_file(decision.path)[0]
        self.store.upsert_downloaded(
            item,
            local_rel_path=self._rel(decision.path),
            local_sha256=sha,
            size_variant=self.config.photo_size,
        )
        mtime = item.create_time_ms / 1000.0 if item.create_time_ms > 0 else None
        self._postprocess(item, decision.path, self._item_datetime(item), mtime, only_missing=True)
        return "adopted", decision.path

    def _process_item(self, item: MediaItem, ctx: _RunContext) -> tuple[str, Path]:
        if self._abort.is_set() or self._stop.is_set():
            raise SyncCancelledError("Download skipped: sync is stopping")

        dt = self._item_datetime(item)
        replacing = item.unique_id in ctx.replace_uids

        def _resolve(allow_adopt: bool) -> TargetDecision:
            return resolve_target(
                self.config.directory,
                folder_structure=self.config.folder_structure,
                file_match_policy=self.config.file_match_policy,
                item=item,
                dt=dt,
                store=self.store,
                reserved=ctx.reserved,
                allow_adopt=allow_adopt,
            )

        # 1. Claim an identical, untracked local file instead of downloading it again
        #    (lost state.db, migration from another tool, crash before the DB write).
        if self._allow_adopt(item, ctx):
            with ctx.path_lock:
                decision = _resolve(True)
                if decision.adopt:
                    ctx.reserved.add(decision.path)
                    return self._adopt(item, decision, ctx)

        # 2. Download into a stable, uid-based part file (resumable across runs).
        part_path = self._part_path(item)
        part_path.parent.mkdir(parents=True, exist_ok=True)
        dl_res = self._download_verified(item, part_path)

        if item.name_synthesized and dl_res.content_disposition_filename:
            item.file_name = Path(dl_res.content_disposition_filename).name
            item.name_synthesized = False
        if item.file_size <= 0:
            item.file_size = dl_res.bytes_written

        # 3. Optional EXIF date injection happens after integrity verification.
        final_sha = dl_res.sha256_hex
        if (
            self.config.set_exif_datetime
            and item.create_time_ms > 0
            and Path(item.file_name).suffix.lower() in {".jpg", ".jpeg"}
        ):
            if inject_exif_datetime_if_missing(part_path, dt):
                final_sha = hash_file(part_path)[0]

        mtime_sec = item.create_time_ms / 1000.0 if item.create_time_ms > 0 else time.time()
        os.utime(part_path, (mtime_sec, mtime_sec))

        # 4. Resolve the final name and commit file + DB record together under the path lock.
        with ctx.path_lock:
            decision = _resolve(self._allow_adopt(item, ctx))
            if decision.adopt:
                part_path.unlink(missing_ok=True)
                ctx.reserved.add(decision.path)
                return self._adopt(item, decision, ctx)
            target_path = decision.path
            ctx.reserved.add(target_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            os.replace(part_path, target_path)
            self.store.upsert_downloaded(
                item,
                local_rel_path=self._rel(target_path),
                local_sha256=final_sha,
                size_variant=self.config.photo_size,
            )

        # 5. Best-effort post-processing: failures never un-commit the backup.
        self._postprocess(item, target_path, dt, mtime_sec)
        return ("changed" if replacing else "downloaded"), target_path

    def _postprocess(
        self,
        item: MediaItem,
        target_path: Path,
        dt: datetime,
        mtime_sec: float | None,
        *,
        only_missing: bool = False,
    ) -> None:
        extras: list[str] = []
        policy = self.config.live_photo_mov_filename_policy
        if policy != "none" and (
            item.is_live_photo or target_path.suffix.lower() in {".jpg", ".jpeg"}
        ):
            out_path = motion_video_path(target_path, policy)
            try:
                owner = self.store.find_by_rel_path(self._rel(out_path))
                if owner is not None and owner["unique_id"] != item.unique_id:
                    logger.warning(
                        "Not extracting Motion Photo video to %s: path belongs to another item",
                        out_path,
                    )
                elif only_missing and out_path.exists():
                    extras.append(self._rel(out_path))
                else:
                    produced = extract_motion_photo_video(target_path, policy, mtime_sec=mtime_sec)
                    if produced is not None:
                        extras.append(self._rel(produced))
            except Exception as exc:
                logger.warning("Motion Photo extraction failed for %s: %s", target_path, exc)

        if self.config.xmp_sidecar:
            xmp_path = Path(f"{target_path}.xmp")
            try:
                if not (only_missing and xmp_path.exists()):
                    write_xmp_sidecar(target_path, item, dt)
                extras.append(self._rel(xmp_path))
            except Exception as exc:
                logger.warning("XMP sidecar write failed for %s: %s", target_path, exc)

        if extras:
            try:
                self.store.add_extra_paths(item.unique_id, extras)
            except Exception as exc:
                logger.warning("Could not record sidecar paths for %s: %s", item.unique_id, exc)

    # ------------------------------------------------------------------ deletion

    def _list_recycle_bin(self) -> dict[str, MediaStub]:
        recycled: dict[str, MediaStub] = {}
        for page in self.client.iter_simple_files(
            RECYCLE_ALBUM_ID,
            album_name="最近删除",
            query_type=4,
            page_size=self.config.page_size,
            request_delay_ms=self.config.request_delay_ms,
        ):
            self._check_stop()
            for stub in page:
                recycled[stub.unique_id] = stub
        return recycled

    def _reconcile_pending_deletes(self, recycled: dict[str, MediaStub], ctx: _RunContext) -> None:
        """Resolve cloud deletions whose outcome was unknown (crash / network error)."""
        pending = self.store.iter_records(status=None, cloud_states=[CLOUD_DELETE_PENDING])
        if not pending or ctx.is_dry:
            return
        confirmed = [r["unique_id"] for r in pending if r["unique_id"] in recycled]
        not_deleted = [r["unique_id"] for r in pending if r["unique_id"] not in recycled]
        self.store.set_cloud_state(confirmed, CLOUD_DELETED)
        self.store.set_cloud_state(not_deleted, CLOUD_DELETE_FAILED)
        logger.info(
            "Reconciled %d pending cloud deletions: %d confirmed in Recycle Bin, %d still in cloud",
            len(pending),
            len(confirmed),
            len(not_deleted),
        )

    def _safe_local_delete(self, rel: str, unique_id: str) -> bool:
        """Delete a local file only if no *other* record claims that path."""
        owner = self.store.find_by_rel_path(rel)
        if owner is not None and owner["unique_id"] != unique_id:
            return False
        path = self.config.directory / rel
        if path.is_file():
            path.unlink(missing_ok=True)
            return True
        return False

    def _run_auto_delete(self, recycled: dict[str, MediaStub], ctx: _RunContext) -> None:
        """Delete local files the *user* moved into the Huawei Cloud Recycle Bin."""
        for uid in recycled:
            rec = self.store.get_record(uid)
            if rec is None or rec["status"] != STATUS_DOWNLOADED:
                continue
            if (rec.get("cloud_state") or CLOUD_PRESENT) != CLOUD_PRESENT:
                # Recycled by hwcloudpd's own move mode (or outcome pending): keep local backup.
                continue
            local_path = self.config.directory / rec["local_rel_path"]
            if ctx.is_dry:
                logger.info("[DRY-RUN] Would delete locally recycled file: %s", local_path)
                ctx.report.deleted_local += 1
                continue

            extra_rels = StateStore.extra_paths_of(rec)
            if not extra_rels:
                # Records written before schema v2 did not track sidecars; derive their names.
                # `_safe_local_delete` never removes a file that another record owns.
                extra_rels = [f"{rec['local_rel_path']}.xmp"]
                for policy in ("suffix", "original"):
                    extra_rels.append(self._rel(motion_video_path(local_path, policy)))
            if local_path.exists():
                local_path.unlink(missing_ok=True)
            for rel in extra_rels:
                if rel != rec["local_rel_path"]:
                    self._safe_local_delete(rel, uid)
            self.store.mark_deleted_local(uid)
            ctx.report.deleted_local += 1
            logger.info("Deleted locally (cloud recycled): %s", local_path)

    def _cleanup_candidates(
        self,
        ctx: _RunContext,
        target_albums: list[AlbumInfo],
    ) -> list[MediaItem]:
        selected_ids = {a.album_id for a in target_albums}
        if self.config.delete_after_download:
            # Items backed up in this run, plus earlier move-mode deletions that failed.
            uids = set(ctx.backed_up_uids)
            uids.update(
                r["unique_id"]
                for r in self.store.iter_records(status=STATUS_DOWNLOADED, cloud_states=[CLOUD_DELETE_FAILED])
            )
            records = [r for uid in uids if (r := self.store.get_record(uid)) is not None]
        else:
            assert self.config.keep_cloud_recent_days is not None
            cutoff_ms = int(
                (time.time() - max(0, self.config.keep_cloud_recent_days) * 86400) * 1000
            )
            records = [
                r
                for r in self.store.iter_records(
                    status=STATUS_DOWNLOADED, cloud_states=[CLOUD_PRESENT, CLOUD_DELETE_FAILED]
                )
                if 0 < int(r["create_time_ms"] or 0) < cutoff_ms
            ]

        candidates: list[MediaItem] = []
        skipped_shared = 0
        for rec in records:
            if rec["status"] != STATUS_DOWNLOADED:
                continue
            if (rec.get("cloud_state") or CLOUD_PRESENT) not in (CLOUD_PRESENT, CLOUD_DELETE_FAILED):
                continue
            if rec["size_variant"] != "original":
                continue
            album_id = rec["album_id"]
            # Respect the *current* album selection / exclusions.
            if album_id in ctx.excluded_ids:
                continue
            if self.config.albums and album_id not in selected_ids:
                # Virtual album selections (favorites, all photos) cover every real album.
                if not any(a.is_virtual for a in target_albums):
                    continue
            album = ctx.album_by_id.get(album_id)
            is_shared = bool(rec.get("is_shared")) or bool(album and album.is_shared)
            if is_shared:
                skipped_shared += 1
                continue
            local = self.config.directory / rec["local_rel_path"]
            try:
                if not local.is_file():
                    continue
                size = int(rec["file_size"] or 0)
                exif_may_differ = self.config.set_exif_datetime and local.suffix.lower() in {".jpg", ".jpeg"}
                if size > 0 and local.stat().st_size != size and not exif_may_differ:
                    logger.warning("Local copy %s does not match cloud size; keeping cloud original", local)
                    continue
            except OSError:
                continue
            candidates.append(
                MediaItem(
                    unique_id=rec["unique_id"],
                    album_id=album_id,
                    album_name=rec["album_name"],
                    file_type=rec["file_type"],
                    create_time_ms=int(rec["create_time_ms"] or 0),
                    file_name=rec["file_name"],
                    file_size=int(rec["file_size"] or 0),
                    album_version=rec.get("album_version") or (album.album_version if album else ""),
                    owner_id=(rec.get("owner_id") or (album.owner_id if album else None)) or None,
                    is_shared=False,
                )
            )
        if skipped_shared:
            logger.warning(
                "Move mode skipped %d item(s) in shared albums (never deleted from the cloud)",
                skipped_shared,
            )
        return candidates

    def _run_cloud_cleanup(self, ctx: _RunContext, target_albums: list[AlbumInfo]) -> None:
        """Execute --delete-after-download or --keep-cloud-recent-days move mode."""
        candidates = self._cleanup_candidates(ctx, target_albums)
        if not candidates:
            return
        if ctx.is_dry:
            ctx.report.deleted_cloud += len(candidates)
            logger.info("[DRY-RUN] Would move %d cloud items to Recycle Bin", len(candidates))
            return

        uids = [c.unique_id for c in candidates]
        # Record intent BEFORE calling the API so a crash / partial failure can never make our
        # own deletions look like user deletions to --auto-delete.
        self.store.set_cloud_state(uids, CLOUD_DELETE_PENDING)
        result = self.client.delete_cloud_files(candidates)
        self.store.set_cloud_state(result.deleted, CLOUD_DELETED)
        self.store.set_cloud_state(result.failed, CLOUD_DELETE_FAILED)
        ctx.report.deleted_cloud += len(result.deleted)
        logger.info(
            "Moved %d backed-up items to Huawei Cloud Recycle Bin (%d rejected, %d unknown)",
            len(result.deleted),
            len(result.failed),
            len(result.unknown),
        )
        if result.aborted_error is not None:
            raise result.aborted_error

    def _mark_missing(self, recycled: dict[str, MediaStub], ctx: _RunContext) -> None:
        """Flag records that are no longer visible anywhere in the cloud (no local deletion)."""
        missing = [
            r["unique_id"]
            for r in self.store.iter_records(status=STATUS_DOWNLOADED, cloud_states=[CLOUD_PRESENT])
            if r["unique_id"] not in ctx.visible_uids and r["unique_id"] not in recycled
        ]
        if not missing:
            return
        ctx.report.cloud_missing += len(missing)
        if not ctx.is_dry:
            self.store.set_cloud_state(missing, CLOUD_MISSING)
        logger.warning(
            "%d backed-up item(s) are no longer in the cloud (not in any album nor the Recycle Bin); "
            "local copies are kept.",
            len(missing),
        )

    # ------------------------------------------------------------------ housekeeping

    def _cleanup_stale_parts(self) -> None:
        part_dir = self.config.directory / PART_DIR_NAME
        if not part_dir.is_dir():
            return
        cutoff = time.time() - STALE_PART_MAX_AGE_SEC
        for part in part_dir.glob("*.hwpart"):
            try:
                if part.stat().st_mtime < cutoff:
                    part.unlink(missing_ok=True)
            except OSError:
                continue

    # ------------------------------------------------------------------ main

    def run_once(self, *, force_full_scan: bool = False) -> SyncReport:
        """Execute a complete synchronization pass and return a SyncReport."""
        started = time.monotonic()
        report = SyncReport(full_scan=force_full_scan)
        is_dry = self.config.dry_run or self.config.only_print_filenames
        self._abort.clear()

        self.config.directory.mkdir(parents=True, exist_ok=True)
        if not is_dry:
            self._cleanup_stale_parts()

        ctx: _RunContext | None = None
        try:
            all_albums = self.client.list_albums(include_stats=False)
            target_albums, excluded_ids, name_by_id = self._select_albums(all_albums)
            ctx = _RunContext(
                report=report,
                is_dry=is_dry,
                full_scan=force_full_scan,
                effective_until_found=None if force_full_scan else self.config.until_found,
                excluded_ids=excluded_ids,
                name_by_id=name_by_id,
                album_by_id={a.album_id: a for a in all_albums},
                recent_remaining=self.config.recent,
            )

            has_pending_deletes = bool(
                self.store.iter_records(status=None, cloud_states=[CLOUD_DELETE_PENDING])
            )
            detect_missing = (
                force_full_scan
                and not self.config.albums
                and self.config.recent is None
                and not self.config.skip_bursts
            )
            recycled: dict[str, MediaStub] | None = None
            if self.config.auto_delete or has_pending_deletes or detect_missing:
                recycled = self._list_recycle_bin()
            if has_pending_deletes and recycled is not None:
                self._reconcile_pending_deletes(recycled, ctx)

            for source in self._build_sources(target_albums):
                if ctx.recent_remaining is not None and ctx.recent_remaining <= 0:
                    break
                self._scan_source(source, ctx)
            self._flush_pending(ctx)
            self._flush_verify(ctx)

            if self.config.auto_delete and recycled is not None:
                self._run_auto_delete(recycled, ctx)

            if detect_missing and recycled is not None:
                self._mark_missing(recycled, ctx)

            if self.config.move_mode:
                self._run_cloud_cleanup(ctx, target_albums)
        except SyncCancelledError:
            report.cancelled = True
            logger.warning("Sync cancelled; progress so far is saved and will resume next run.")
        finally:
            if ctx is not None and ctx.pool is not None:
                ctx.pool.shutdown(wait=True, cancel_futures=True)
            report.duration_sec = time.monotonic() - started
        return report
