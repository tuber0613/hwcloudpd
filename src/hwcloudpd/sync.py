"""Core synchronization engine implementing icloudpd-parity workflows."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import fnmatch
import logging
import os
from pathlib import Path
import threading
import time
from typing import Callable
from zoneinfo import ZoneInfo

from hwcloudpd.client import SYSTEM_ALBUM_NAMES, VIRTUAL_ALBUM_TYPE_MAP, HuaweiGalleryClient
from hwcloudpd.exif import inject_exif_datetime_if_missing
from hwcloudpd.models import (
    AlbumInfo,
    AuthExpiredError,
    HwCloudError,
    MediaItem,
    MediaStub,
    SyncConfig,
    SyncReport,
)
from hwcloudpd.postprocess import (
    extract_motion_photo_video,
    resolve_target_path,
    write_xmp_sidecar,
)
from hwcloudpd.store import StateStore

logger = logging.getLogger(__name__)


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


class SyncEngine:
    """Orchestrates album discovery, incremental filtering, atomic download, and sync/move modes."""

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

    def _select_albums(
        self, all_albums: list[AlbumInfo]
    ) -> tuple[list[AlbumInfo], set[str], dict[str, str]]:
        """Select target albums (default: all non-recycle albums) plus exclusion metadata."""
        name_by_id = {a.album_id: a.name for a in all_albums}
        name_by_id.update(SYSTEM_ALBUM_NAMES)

        excluded_ids: set[str] = {"default-album-3"}
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
                    is_recycle=(vid == "default-album-3"),
                )
                for vid in VIRTUAL_ALBUM_TYPE_MAP
                if vid != "default-album-3"
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

    def _expand_burst_if_needed(self, stub: MediaStub) -> list[MediaStub]:
        if stub.is_cover != 1 or self.config.skip_bursts:
            return [stub]
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
                return bursts
        except AuthExpiredError:
            raise
        except HwCloudError as exc:
            logger.warning("Failed to expand burst %s: %s", stub.unique_id, exc)
        return [stub]

    def _collect_pending_from_source(
        self,
        *,
        album_id: str | None,
        album_name: str,
        album_version: str,
        owner_id: str | None,
        query_type: int | None,
        excluded_ids: set[str],
        name_by_id: dict[str, str],
        seen_uids: set[str],
        previously_synced_uids: set[str],
        effective_until_found: int | None,
        recent_remaining: list[int | None],
        report: SyncReport,
    ) -> list[MediaStub]:
        pending: list[MediaStub] = []
        consecutive_found = 0

        for page in self.client.iter_simple_files(
            album_id,
            album_name=album_name,
            album_version=album_version,
            owner_id=owner_id,
            query_type=query_type,
            page_size=self.config.page_size,
            request_delay_ms=self.config.request_delay_ms,
        ):
            stop_source = False
            for raw_stub in page:
                if raw_stub.album_id in excluded_ids:
                    continue
                if raw_stub.album_id in name_by_id:
                    raw_stub.album_name = name_by_id[raw_stub.album_id]

                for stub in self._expand_burst_if_needed(raw_stub):
                    if stub.album_id in excluded_ids:
                        continue
                    if stub.unique_id in seen_uids:
                        continue
                    seen_uids.add(stub.unique_id)
                    report.scanned += 1

                    if recent_remaining[0] is not None:
                        if recent_remaining[0] <= 0:
                            stop_source = True
                            break
                        recent_remaining[0] -= 1

                    if not self._passes_stub_filters(stub):
                        report.skipped_filtered += 1
                        continue

                    if stub.unique_id in previously_synced_uids or self.store.is_synced_and_present(
                        stub.unique_id,
                        self.config.directory,
                        self.config.photo_size,
                    ):
                        previously_synced_uids.add(stub.unique_id)
                        report.skipped_existing += 1
                        consecutive_found += 1
                        if (
                            effective_until_found is not None
                            and consecutive_found >= effective_until_found
                        ):
                            logger.debug(
                                "Early-stop triggered for album=%s after %d existing items",
                                album_id or "timeline",
                                consecutive_found,
                            )
                            stop_source = True
                            break
                        continue

                    consecutive_found = 0
                    pending.append(stub)

                if stop_source:
                    break
            if stop_source:
                break

        return pending

    def _download_single(
        self,
        item: MediaItem,
        *,
        path_lock: threading.Lock,
        reserved_paths: set[Path],
    ) -> Path:
        dt = (
            datetime.fromtimestamp(item.create_time_ms / 1000.0, tz=self._tz)
            if item.create_time_ms > 0
            else datetime.now(tz=self._tz)
        )

        with path_lock:
            target_path = resolve_target_path(
                self.config.directory,
                folder_structure=self.config.folder_structure,
                file_match_policy=self.config.file_match_policy,
                item=item,
                dt=dt,
                store=self.store,
            )
            if target_path in reserved_paths:
                base_stem = target_path.stem
                target_path = target_path.with_name(
                    f"{base_stem}-{item.unique_id[:7]}{target_path.suffix}"
                )
                if target_path in reserved_paths:
                    target_path = target_path.with_name(
                        f"{base_stem}-{item.unique_id[:12]}{target_path.suffix}"
                    )
            reserved_paths.add(target_path)

        target_path.parent.mkdir(parents=True, exist_ok=True)
        part_path = target_path.with_name(f".{target_path.name}.{item.unique_id[:7]}.hwpart")

        last_err: Exception | None = None
        dl_res = None
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
                    if len(item.checksum) == 64 and dl_res.sha256_hex != item.checksum.lower():
                        part_path.unlink(missing_ok=True)
                        raise HwCloudError(
                            f"SHA256 mismatch for {item.file_name}: "
                            f"expected {item.checksum}, got {dl_res.sha256_hex}"
                        )
                    if len(item.checksum) == 32 and dl_res.md5_hex != item.checksum.lower():
                        part_path.unlink(missing_ok=True)
                        raise HwCloudError(
                            f"MD5 mismatch for {item.file_name}: "
                            f"expected {item.checksum}, got {dl_res.md5_hex}"
                        )
                break
            except AuthExpiredError:
                raise
            except Exception as exc:
                last_err = exc
                if attempt == 0:
                    logger.warning("Retrying %s after error: %s", item.file_name, exc)
                    time.sleep(0.5)
                    continue
                raise

        assert dl_res is not None
        if item.file_size <= 0:
            item.file_size = dl_res.bytes_written

        # Verify integrity BEFORE mutating JPEG bytes for EXIF injection.
        if (
            self.config.set_exif_datetime
            and item.create_time_ms > 0
            and target_path.suffix.lower() in {".jpg", ".jpeg"}
        ):
            inject_exif_datetime_if_missing(part_path, dt)

        mtime_sec = item.create_time_ms / 1000.0 if item.create_time_ms > 0 else time.time()
        os.utime(part_path, (mtime_sec, mtime_sec))
        os.replace(part_path, target_path)

        if self.config.live_photo_mov_filename_policy != "none" and (
            item.is_live_photo or target_path.suffix.lower() in {".jpg", ".jpeg"}
        ):
            extract_motion_photo_video(
                target_path,
                self.config.live_photo_mov_filename_policy,
                mtime_sec=mtime_sec,
            )

        if self.config.xmp_sidecar:
            write_xmp_sidecar(target_path, item, dt)

        rel_path = str(target_path.relative_to(self.config.directory))
        self.store.upsert_downloaded(
            item,
            local_rel_path=rel_path,
            local_sha256=dl_res.sha256_hex,
            size_variant=self.config.photo_size,
        )
        return target_path

    def _run_auto_delete(self, report: SyncReport) -> None:
        """Delete local files that were moved into Huawei Cloud Recycle Bin (`default-album-3`)."""
        is_dry = self.config.dry_run or self.config.only_print_filenames
        for page in self.client.iter_simple_files(
            "default-album-3",
            album_name="最近删除",
            query_type=4,
            page_size=self.config.page_size,
            request_delay_ms=self.config.request_delay_ms,
        ):
            for stub in page:
                rec = self.store.get_record(stub.unique_id)
                if rec is None or rec["status"] != "downloaded":
                    continue
                local_path = self.config.directory / rec["local_rel_path"]
                if is_dry:
                    logger.info("[DRY-RUN] Would delete locally recycled file: %s", local_path)
                    report.deleted_local += 1
                    continue
                if local_path.exists():
                    local_path.unlink(missing_ok=True)
                xmp_path = Path(f"{local_path}.xmp")
                xmp_path.unlink(missing_ok=True)
                hevc_mov = local_path.with_name(f"{local_path.stem}_HEVC.mp4")
                hevc_mov.unlink(missing_ok=True)
                if rec["file_type"] == "5":
                    orig_mov = local_path.with_suffix(".mp4")
                    orig_mov.unlink(missing_ok=True)
                self.store.mark_deleted_local(stub.unique_id)
                report.deleted_local += 1
                logger.info("Deleted locally (cloud recycled): %s", local_path)

    def _run_cloud_cleanup(self, downloaded_items: list[MediaItem], report: SyncReport) -> None:
        """Execute --delete-after-download or --keep-cloud-recent-days move mode."""
        is_dry = self.config.dry_run or self.config.only_print_filenames
        candidates: list[MediaItem] = []

        if self.config.delete_after_download:
            candidates = list(downloaded_items)
        elif self.config.keep_cloud_recent_days is not None:
            cutoff_ms = int(
                (time.time() - max(0, self.config.keep_cloud_recent_days) * 86400) * 1000
            )
            for rec in self.store.iter_downloaded_records():
                if not (self.config.directory / rec["local_rel_path"]).is_file():
                    continue
                ctime = int(rec["create_time_ms"] or 0)
                if ctime <= 0 or ctime >= cutoff_ms:
                    continue
                candidates.append(
                    MediaItem(
                        unique_id=rec["unique_id"],
                        album_id=rec["album_id"],
                        album_name=rec["album_name"],
                        file_type=rec["file_type"],
                        create_time_ms=ctime,
                        file_name=rec["file_name"],
                        file_size=int(rec["file_size"] or 0),
                    )
                )

        if not candidates:
            return

        if is_dry:
            report.deleted_cloud += len(candidates)
            logger.info("[DRY-RUN] Would move %d cloud items to Recycle Bin", len(candidates))
            return

        deleted = self.client.delete_cloud_files(candidates)
        for item in candidates:
            self.store.mark_deleted_cloud(item.unique_id)
        report.deleted_cloud += deleted
        logger.info("Moved %d backed-up items to Huawei Cloud Recycle Bin", deleted)

    def run_once(self, *, force_full_scan: bool = False) -> SyncReport:
        """Execute a complete synchronization pass and return a SyncReport."""
        started = time.monotonic()
        report = SyncReport()
        is_dry = self.config.dry_run or self.config.only_print_filenames
        effective_until_found = None if force_full_scan else self.config.until_found

        self.config.directory.mkdir(parents=True, exist_ok=True)
        all_albums = self.client.list_albums(include_stats=False)
        target_albums, excluded_ids, name_by_id = self._select_albums(all_albums)

        seen_uids: set[str] = set()
        previously_synced_uids: set[str] = set()
        recent_remaining: list[int | None] = [self.config.recent]
        pending_stubs: list[MediaStub] = []

        for album in target_albums:
            if recent_remaining[0] is not None and recent_remaining[0] <= 0:
                break
            stubs = self._collect_pending_from_source(
                album_id=album.album_id,
                album_name=album.name,
                album_version=album.album_version,
                owner_id=album.owner_id,
                query_type=None,
                excluded_ids=excluded_ids,
                name_by_id=name_by_id,
                seen_uids=seen_uids,
                previously_synced_uids=previously_synced_uids,
                effective_until_found=effective_until_found,
                recent_remaining=recent_remaining,
                report=report,
            )
            pending_stubs.extend(stubs)

        # In default all-album mode, also sweep global timeline so items not assigned to
        # queryAlbumInfo albums (or unassigned timeline entries) are never missed.
        if not self.config.albums and (
            recent_remaining[0] is None or recent_remaining[0] > 0
        ):
            timeline_stubs = self._collect_pending_from_source(
                album_id=None,
                album_name="",
                album_version="",
                owner_id=None,
                query_type=None,
                excluded_ids=excluded_ids,
                name_by_id=name_by_id,
                seen_uids=seen_uids,
                previously_synced_uids=previously_synced_uids,
                effective_until_found=effective_until_found,
                recent_remaining=recent_remaining,
                report=report,
            )
            pending_stubs.extend(timeline_stubs)

        items_to_download: list[MediaItem] = []
        if pending_stubs:
            enriched_items = self.client.enrich_metadata(pending_stubs)
            for item in enriched_items:
                if not self._passes_size_filters(item):
                    report.skipped_filtered += 1
                    continue
                items_to_download.append(item)

        downloaded_items: list[MediaItem] = []
        if items_to_download:
            if is_dry:
                dry_reserved: set[Path] = set()
                for item in items_to_download:
                    dt = (
                        datetime.fromtimestamp(item.create_time_ms / 1000.0, tz=self._tz)
                        if item.create_time_ms > 0
                        else datetime.now(tz=self._tz)
                    )
                    target = resolve_target_path(
                        self.config.directory,
                        folder_structure=self.config.folder_structure,
                        file_match_policy=self.config.file_match_policy,
                        item=item,
                        dt=dt,
                        store=self.store,
                    )
                    if target in dry_reserved:
                        base_stem = target.stem
                        target = target.with_name(
                            f"{base_stem}-{item.unique_id[:7]}{target.suffix}"
                        )
                        if target in dry_reserved:
                            target = target.with_name(
                                f"{base_stem}-{item.unique_id[:12]}{target.suffix}"
                            )
                    dry_reserved.add(target)
                    if self.config.only_print_filenames:
                        self.output_fn(str(target))
                    else:
                        logger.info("[DRY-RUN] Would download %s -> %s", item.file_name, target)
                    report.downloaded += 1
            else:
                self.client.pre_process()
                path_lock = threading.Lock()
                reserved_paths: set[Path] = set()
                workers = max(1, self.config.download_concurrency)

                if workers == 1:
                    for item in items_to_download:
                        try:
                            target = self._download_single(
                                item, path_lock=path_lock, reserved_paths=reserved_paths
                            )
                            downloaded_items.append(item)
                            report.downloaded += 1
                            logger.info("Downloaded %s", target)
                        except AuthExpiredError:
                            raise
                        except Exception as exc:
                            report.failed += 1
                            logger.error("Failed to download %s (%s): %s", item.file_name, item.unique_id, exc)
                else:
                    with ThreadPoolExecutor(max_workers=workers) as pool:
                        future_map = {
                            pool.submit(
                                self._download_single,
                                item,
                                path_lock=path_lock,
                                reserved_paths=reserved_paths,
                            ): item
                            for item in items_to_download
                        }
                        for fut in as_completed(future_map):
                            item = future_map[fut]
                            try:
                                target = fut.result()
                                downloaded_items.append(item)
                                report.downloaded += 1
                                logger.info("Downloaded %s", target)
                            except AuthExpiredError:
                                raise
                            except Exception as exc:
                                report.failed += 1
                                logger.error(
                                    "Failed to download %s (%s): %s",
                                    item.file_name,
                                    item.unique_id,
                                    exc,
                                )

        if self.config.auto_delete:
            self._run_auto_delete(report)

        if self.config.delete_after_download or self.config.keep_cloud_recent_days is not None:
            self._run_cloud_cleanup(downloaded_items, report)

        report.duration_sec = time.monotonic() - started
        return report
