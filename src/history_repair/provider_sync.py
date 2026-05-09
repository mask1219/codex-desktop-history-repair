from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import shutil
import sqlite3
import time
import tomllib
from typing import Any


@dataclass(frozen=True)
class RolloutSession:
    thread_id: str
    path: Path
    archived: bool
    provider: str | None
    cwd: str | None
    encrypted_content_count: int


@dataclass(frozen=True)
class ThreadRow:
    thread_id: str
    rollout_path: str
    provider: str
    cwd: str
    archived: bool
    updated_at_ms: int
    source: str | None
    first_user_message: str | None


@dataclass(frozen=True)
class WorkspaceRootReport:
    present: bool
    updated: bool
    updated_workspace_roots: int
    saved_workspace_root_count: int
    missing_workspace_roots: list[str]


@dataclass(frozen=True)
class SyncPreviewItem:
    thread_id: str
    rollout_path: str | None
    db_rollout_path: str | None
    rollout_provider: str | None
    db_provider: str | None
    target_provider: str | None
    rollout_cwd: str | None
    db_cwd: str | None
    target_cwd: str | None
    rollout_archived: bool | None
    db_archived: bool | None
    missing_rollout: bool
    missing_db_row: bool
    needs_rollout_update: bool
    needs_db_update: bool


@dataclass(frozen=True)
class StatusReport:
    codex_home: str
    current_provider: str | None
    total_rollouts: int
    total_db_threads: int
    matched_threads: int
    missing_rollout_count: int
    missing_db_count: int
    provider_mismatch_count: int
    cwd_mismatch_count: int
    path_mismatch_count: int
    archived_mismatch_count: int
    locked_rollout_files: list[str]
    global_state_present: bool
    workspace_root_count: int
    missing_workspace_roots: list[str]
    project_visibility: list[dict[str, Any]]
    encrypted_content_threads: int
    encrypted_content_items: int
    encrypted_content_preview: list[dict[str, Any]]
    preview: list[dict[str, Any]]


@dataclass(frozen=True)
class SyncReport:
    codex_home: str
    target_provider: str | None
    target_cwd: str | None
    dry_run: bool
    changed: bool
    backup_dir: str | None
    scanned_threads: int
    updated_rollouts: int
    updated_db_rows: int
    updated_workspace_roots: int
    saved_workspace_root_count: int
    skipped_missing_rollout: int
    skipped_missing_db_rows: int
    skipped_locked_rollouts: list[str]
    encrypted_content_threads: int
    encrypted_content_items: int
    encrypted_content_preview: list[dict[str, Any]]
    preview: list[dict[str, Any]]


@dataclass(frozen=True)
class SwitchReport:
    codex_home: str
    provider: str
    model: str | None
    dry_run: bool
    config_updated: bool
    backup_dir: str | None
    sync: SyncReport | None


@dataclass(frozen=True)
class RestoreReport:
    codex_home: str
    backup_dir: str
    restored_files: int
    restored_config: int
    restored_database: int
    restored_sessions: int
    restored_global_state: int


@dataclass(frozen=True)
class PruneReport:
    codex_home: str
    kept: int
    removed_backups: list[str]


@dataclass(frozen=True)
class HideStaleWorkspacesReport:
    codex_home: str
    dry_run: bool
    changed: bool
    backup_dir: str | None
    matched_thread_ids: list[str]
    removed_session_index_entries: list[str]
    removed_thread_rows: int
    removed_workspace_roots: list[str]
    hidden_workspace_roots: list[str]
    hidden_archived_rollouts: list[str]
    hidden_removed_rollouts: list[str]


@dataclass(frozen=True)
class ElectronStateCleanupReport:
    app_support_root: str
    dry_run: bool
    changed: bool
    backup_dir: str | None
    removed_paths: list[str]
    missing_paths: list[str]


class BackupManager:
    def __init__(self, codex_home: Path):
        self.codex_home = codex_home
        self.backups_root = codex_home / "backups_provider_sync"

    def create(self, files: list[Path]) -> Path:
        timestamp = time.strftime("%Y%m%d%H%M%S", time.localtime())
        backup_dir = self.backups_root / timestamp
        suffix = 0
        while backup_dir.exists():
            suffix += 1
            backup_dir = self.backups_root / f"{timestamp}-{suffix}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        manifest: list[str] = []
        seen: set[Path] = set()
        for file_path in files:
            for candidate in _backup_companion_paths(file_path):
                if candidate in seen or not candidate.exists():
                    continue
                seen.add(candidate)
                relative = candidate.relative_to(self.codex_home)
                destination = backup_dir / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(candidate, destination)
                manifest.append(relative.as_posix())
        (backup_dir / "manifest.json").write_text(
            json.dumps({"created_at": timestamp, "files": manifest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return backup_dir

    def restore(
        self,
        backup_dir: Path,
        *,
        include_config: bool = True,
        include_database: bool = True,
        include_sessions: bool = True,
        include_global_state: bool = True,
    ) -> RestoreReport:
        manifest_path = backup_dir / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        restored = 0
        restored_config = 0
        restored_database = 0
        restored_sessions = 0
        restored_global_state = 0
        for relative in manifest.get("files", []):
            category = _restore_category(str(relative))
            if category == "config" and not include_config:
                continue
            if category == "database" and not include_database:
                continue
            if category == "sessions" and not include_sessions:
                continue
            if category == "global_state" and not include_global_state:
                continue
            source = backup_dir / relative
            destination = self.codex_home / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            restored += 1
            if category == "config":
                restored_config += 1
            elif category == "database":
                restored_database += 1
            elif category == "sessions":
                restored_sessions += 1
            elif category == "global_state":
                restored_global_state += 1
        return RestoreReport(
            codex_home=str(self.codex_home),
            backup_dir=str(backup_dir),
            restored_files=restored,
            restored_config=restored_config,
            restored_database=restored_database,
            restored_sessions=restored_sessions,
            restored_global_state=restored_global_state,
        )

    def prune(self, keep: int) -> PruneReport:
        if keep < 0:
            raise ValueError("keep must be >= 0")
        if not self.backups_root.exists():
            return PruneReport(codex_home=str(self.codex_home), kept=keep, removed_backups=[])
        entries = sorted(
            [path for path in self.backups_root.iterdir() if path.is_dir()],
            key=lambda path: path.name,
            reverse=True,
        )
        removed: list[str] = []
        for path in entries[keep:]:
            shutil.rmtree(path)
            removed.append(str(path))
        return PruneReport(codex_home=str(self.codex_home), kept=keep, removed_backups=removed)


class CodexProviderSyncService:
    def __init__(self, codex_home: str | Path | None = None):
        base = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
        self.codex_home = base.resolve()
        self.app_support_root = (Path.home() / "Library" / "Application Support" / "Codex").resolve()
        self.config_path = self.codex_home / "config.toml"
        self.db_path = self.codex_home / "state_5.sqlite"
        self.sessions_root = self.codex_home / "sessions"
        self.archived_root = self.codex_home / "archived_sessions"
        self.hidden_state_root = self.codex_home.parent / ".codex-history-repair-hidden"
        self.hidden_archived_root = self.hidden_state_root / "archived_sessions"
        self.hidden_removed_root = self.hidden_state_root / "removed_sessions"
        self.hidden_workspace_root = self.hidden_state_root / "workspace_roots"
        self.legacy_hidden_archived_root = self.codex_home / "hidden_archived_sessions"
        self.legacy_hidden_removed_root = self.codex_home / "hidden_removed_sessions"
        self.legacy_hidden_workspace_root = self.codex_home / "hidden_workspace_roots"
        self.global_state_path = self.codex_home / ".codex-global-state.json"
        self.session_index_path = self.codex_home / "session_index.jsonl"
        self.backups = BackupManager(self.codex_home)

    def clear_electron_persisted_state(
        self,
        *,
        dry_run: bool = False,
        app_support_root: str | Path | None = None,
    ) -> ElectronStateCleanupReport:
        root = Path(app_support_root).expanduser() if app_support_root else self.app_support_root
        root = root.resolve()
        target_paths = [
            root / "Local Storage",
            root / "Session Storage",
            root / "Partitions" / "codex-browser-app" / "Local Storage",
            root / "Partitions" / "codex-browser-app" / "Session Storage",
        ]
        existing_paths = [path for path in target_paths if path.exists()]
        missing_paths = [str(path) for path in target_paths if not path.exists()]
        if not existing_paths:
            return ElectronStateCleanupReport(
                app_support_root=str(root),
                dry_run=dry_run,
                changed=False,
                backup_dir=None,
                removed_paths=[],
                missing_paths=missing_paths,
            )
        backup_dir = root / f"history-repair-electron-state-backup-{time.strftime('%Y%m%d%H%M%S', time.localtime())}"
        suffix = 0
        while backup_dir.exists():
            suffix += 1
            backup_dir = root / f"history-repair-electron-state-backup-{time.strftime('%Y%m%d%H%M%S', time.localtime())}-{suffix}"
        if not dry_run:
            backup_dir.mkdir(parents=True, exist_ok=False)
            for path in existing_paths:
                destination = backup_dir / path.relative_to(root)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(destination))
            manifest = {
                "created_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                "removed_paths": [str(path.relative_to(root)) for path in existing_paths],
            }
            (backup_dir / "manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return ElectronStateCleanupReport(
            app_support_root=str(root),
            dry_run=dry_run,
            changed=True,
            backup_dir=None if dry_run else str(backup_dir),
            removed_paths=[str(path) for path in existing_paths],
            missing_paths=missing_paths,
        )

    def current_provider(self) -> str | None:
        if not self.config_path.exists():
            return None
        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        provider = data.get("model_provider")
        return str(provider) if provider else None

    def latest_history_provider(self) -> str | None:
        candidates: list[tuple[int, str]] = []
        rows = self._load_thread_rows()
        latest_row = max(rows.values(), key=lambda row: row.updated_at_ms, default=None)
        if latest_row and latest_row.provider:
            candidates.append((latest_row.updated_at_ms, latest_row.provider))
        rollouts = self._load_rollouts()
        for rollout in rollouts.values():
            if rollout.provider:
                candidates.append((_file_mtime_ms(rollout.path), rollout.provider))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def provider_table_missing(self, provider: str) -> bool:
        if not self.config_path.exists():
            return False
        text = self.config_path.read_text(encoding="utf-8")
        return _provider_table_range(text, provider) is None

    def status(
        self,
        *,
        thread_ids: list[str] | None = None,
        target_provider: str | None = None,
        limit: int = 20,
    ) -> StatusReport:
        rollouts = self._load_rollouts()
        rows = self._load_thread_rows()
        target = target_provider or self.current_provider()
        workspace_roots = _workspace_roots_from_rows(rows.values())
        global_state = self._load_global_state()
        missing_workspace_roots = _missing_workspace_roots(global_state, workspace_roots)
        locked_rollouts = _locked_rollout_files(rollouts.values())
        visibility = _project_visibility(rows.values(), limit=limit)
        encrypted_content = _encrypted_content_summary(rollouts.values(), limit=limit)
        preview: list[dict[str, Any]] = []
        missing_rollout = 0
        missing_db = 0
        provider_mismatch = 0
        cwd_mismatch = 0
        path_mismatch = 0
        archived_mismatch = 0
        matched = 0
        for item in self._build_preview_items(
            rollouts=rollouts,
            rows=rows,
            thread_ids=thread_ids,
            target_provider=target,
            target_cwd=None,
        ):
            if item.missing_rollout:
                missing_rollout += 1
            if item.missing_db_row:
                missing_db += 1
            if item.rollout_provider and item.db_provider and item.rollout_provider == item.db_provider:
                matched += 1
            if item.rollout_provider != item.db_provider or (
                target and ((item.rollout_provider and item.rollout_provider != target) or (item.db_provider and item.db_provider != target))
            ):
                provider_mismatch += 1
            if item.rollout_cwd != item.db_cwd:
                cwd_mismatch += 1
            if item.rollout_path != item.db_rollout_path:
                path_mismatch += 1
            if item.rollout_archived != item.db_archived:
                archived_mismatch += 1
            if len(preview) < limit and (
                item.missing_rollout
                or item.missing_db_row
                or item.rollout_provider != item.db_provider
                or item.rollout_cwd != item.db_cwd
                or item.rollout_path != item.db_rollout_path
                or item.rollout_archived != item.db_archived
            ):
                preview.append(_preview_item_to_dict(item))
        return StatusReport(
            codex_home=str(self.codex_home),
            current_provider=target,
            total_rollouts=len(rollouts),
            total_db_threads=len(rows),
            matched_threads=matched,
            missing_rollout_count=missing_rollout,
            missing_db_count=missing_db,
            provider_mismatch_count=provider_mismatch,
            cwd_mismatch_count=cwd_mismatch,
            path_mismatch_count=path_mismatch,
            archived_mismatch_count=archived_mismatch,
            locked_rollout_files=[str(path) for path in locked_rollouts],
            global_state_present=global_state is not None,
            workspace_root_count=len(workspace_roots),
            missing_workspace_roots=missing_workspace_roots,
            project_visibility=visibility,
            encrypted_content_threads=encrypted_content["threads"],
            encrypted_content_items=encrypted_content["items"],
            encrypted_content_preview=encrypted_content["preview"],
            preview=preview,
        )

    def sync(
        self,
        *,
        target_provider: str | None = None,
        target_cwd: str | None = None,
        thread_ids: list[str] | None = None,
        dry_run: bool = False,
        _create_backup: bool = True,
    ) -> SyncReport:
        provider = target_provider or self.current_provider()
        if not provider:
            raise RuntimeError("target provider is required when config.toml has no model_provider")
        rollouts = self._load_rollouts()
        rows = self._load_thread_rows()
        encrypted_content = _encrypted_content_summary(rollouts.values(), limit=20)
        preview_items = self._build_preview_items(
            rollouts=rollouts,
            rows=rows,
            thread_ids=thread_ids,
            target_provider=provider,
            target_cwd=target_cwd,
        )
        changed_rollouts = [
            item
            for item in preview_items
            if item.needs_rollout_update and not item.missing_rollout and not _is_rollout_locked(Path(item.rollout_path or ""))
        ]
        skipped_locked_rollouts = [
            str(item.rollout_path)
            for item in preview_items
            if item.needs_rollout_update and item.rollout_path and _is_rollout_locked(Path(item.rollout_path))
        ]
        changed_rows = [item for item in preview_items if item.needs_db_update and not item.missing_db_row]
        files_to_backup = [Path(item.rollout_path) for item in changed_rollouts if item.rollout_path]
        if changed_rows:
            files_to_backup.append(self.db_path)
        workspace_report = self._workspace_root_sync_report(rows.values(), dry_run=True)
        if workspace_report.updated:
            files_to_backup.append(self.global_state_path)
        backup_dir: Path | None = None
        if not dry_run and _create_backup and (changed_rollouts or changed_rows or workspace_report.updated):
            backup_dir = self.backups.create(files_to_backup)
        if not dry_run:
            for item in changed_rollouts:
                rollout = rollouts[item.thread_id]
                self._rewrite_rollout(
                    rollout.path,
                    provider=provider,
                    cwd=target_cwd,
                )
            if changed_rows:
                self._update_thread_rows(
                    changed_rows,
                    target_provider=provider,
                    target_cwd=target_cwd,
                )
            workspace_report = self._sync_workspace_roots(rows.values())
        return SyncReport(
            codex_home=str(self.codex_home),
            target_provider=provider,
            target_cwd=target_cwd,
            dry_run=dry_run,
            changed=bool(changed_rollouts or changed_rows or workspace_report.updated),
            backup_dir=str(backup_dir) if backup_dir else None,
            scanned_threads=len(preview_items),
            updated_rollouts=0 if dry_run else len(changed_rollouts),
            updated_db_rows=0 if dry_run else len(changed_rows),
            updated_workspace_roots=0 if dry_run else workspace_report.updated_workspace_roots,
            saved_workspace_root_count=workspace_report.saved_workspace_root_count,
            skipped_missing_rollout=sum(1 for item in preview_items if item.missing_rollout),
            skipped_missing_db_rows=sum(1 for item in preview_items if item.missing_db_row),
            skipped_locked_rollouts=skipped_locked_rollouts,
            encrypted_content_threads=encrypted_content["threads"],
            encrypted_content_items=encrypted_content["items"],
            encrypted_content_preview=encrypted_content["preview"],
            preview=[_preview_item_to_dict(item) for item in preview_items[:20]],
        )

    def switch_provider(
        self,
        *,
        provider: str,
        model: str | None = None,
        target_cwd: str | None = None,
        thread_ids: list[str] | None = None,
        sync_history: bool = True,
        dry_run: bool = False,
        allow_implicit_provider: bool = False,
    ) -> SwitchReport:
        if not allow_implicit_provider:
            self._validate_provider_defined(provider)
        config_updated = self._config_needs_update(
            provider=provider,
            model=model,
            require_provider_table=allow_implicit_provider,
        )
        files_to_backup = [self.config_path] if config_updated and self.config_path.exists() else []
        sync_preview = None
        preview_items: list[SyncPreviewItem] = []
        if sync_history:
            rollouts = self._load_rollouts()
            rows = self._load_thread_rows()
            encrypted_content = _encrypted_content_summary(rollouts.values(), limit=20)
            preview_items = self._build_preview_items(
                rollouts=rollouts,
                rows=rows,
                thread_ids=thread_ids,
                target_provider=provider,
                target_cwd=target_cwd,
            )
            changed_rollouts = [
                Path(item.rollout_path)
                for item in preview_items
                if item.needs_rollout_update
                and item.rollout_path
                and not item.missing_rollout
                and not _is_rollout_locked(Path(item.rollout_path))
            ]
            changed_rows = [item for item in preview_items if item.needs_db_update and not item.missing_db_row]
            skipped_locked_rollouts = [
                str(item.rollout_path)
                for item in preview_items
                if item.needs_rollout_update and item.rollout_path and _is_rollout_locked(Path(item.rollout_path))
            ]
            workspace_report = self._workspace_root_sync_report(rows.values(), dry_run=True)
            sync_preview = SyncReport(
                codex_home=str(self.codex_home),
                target_provider=provider,
                target_cwd=target_cwd,
                dry_run=True,
                changed=bool(changed_rollouts or changed_rows or workspace_report.updated),
                backup_dir=None,
                scanned_threads=len(preview_items),
                updated_rollouts=0,
                updated_db_rows=0,
                updated_workspace_roots=0,
                saved_workspace_root_count=workspace_report.saved_workspace_root_count,
                skipped_missing_rollout=sum(1 for item in preview_items if item.missing_rollout),
                skipped_missing_db_rows=sum(1 for item in preview_items if item.missing_db_row),
                skipped_locked_rollouts=skipped_locked_rollouts,
                encrypted_content_threads=encrypted_content["threads"],
                encrypted_content_items=encrypted_content["items"],
                encrypted_content_preview=encrypted_content["preview"],
                preview=[_preview_item_to_dict(item) for item in preview_items[:20]],
            )
            if sync_preview.changed:
                files_to_backup.append(self.db_path)
                files_to_backup.extend(changed_rollouts)
                if workspace_report.updated:
                    files_to_backup.append(self.global_state_path)
        backup_dir: Path | None = None
        unique_backup_files = _dedupe_paths(files_to_backup)
        if not dry_run and unique_backup_files:
            backup_dir = self.backups.create(unique_backup_files)
        if not dry_run and config_updated:
            self._rewrite_config(
                provider=provider,
                model=model,
                copy_current_provider_config=allow_implicit_provider,
            )
        sync_result = None
        if sync_history:
            sync_result = self.sync(
                target_provider=provider,
                target_cwd=target_cwd,
                thread_ids=thread_ids,
                dry_run=dry_run,
                _create_backup=False,
            )
        return SwitchReport(
            codex_home=str(self.codex_home),
            provider=provider,
            model=model,
            dry_run=dry_run,
            config_updated=config_updated and not dry_run,
            backup_dir=str(backup_dir) if backup_dir else None,
            sync=sync_result,
        )

    def restore(
        self,
        backup_dir: str | Path,
        *,
        include_config: bool = True,
        include_database: bool = True,
        include_sessions: bool = True,
        include_global_state: bool = True,
    ) -> RestoreReport:
        return self.backups.restore(
            Path(backup_dir),
            include_config=include_config,
            include_database=include_database,
            include_sessions=include_sessions,
            include_global_state=include_global_state,
        )

    def prune_backups(self, *, keep: int) -> PruneReport:
        return self.backups.prune(keep)

    def hide_stale_workspaces(self, *, dry_run: bool = False) -> HideStaleWorkspacesReport:
        rows = self._load_thread_rows()
        rollouts = self._load_rollouts()
        archived_rollouts = sorted(self.archived_root.glob("rollout-*.jsonl")) if self.archived_root.exists() else []
        archived_rollout_ids = {rollout.thread_id for rollout in rollouts.values() if rollout.archived}
        legacy_hidden_archived_rollouts = sorted(self.legacy_hidden_archived_root.glob("rollout-*.jsonl")) if self.legacy_hidden_archived_root.exists() else []
        legacy_hidden_removed_rollouts = sorted(self.legacy_hidden_removed_root.glob("rollout-*.jsonl")) if self.legacy_hidden_removed_root.exists() else []
        legacy_hidden_workspace_roots = sorted(self.legacy_hidden_workspace_root.iterdir()) if self.legacy_hidden_workspace_root.exists() else []
        hidden_rollout_ids = (
            _rollout_thread_ids(self.hidden_archived_root)
            | _rollout_thread_ids(self.hidden_removed_root)
            | _rollout_thread_ids(self.legacy_hidden_archived_root)
            | _rollout_thread_ids(self.legacy_hidden_removed_root)
        )
        stale_thread_ids = [
            row.thread_id
            for row in rows.values()
            if row.archived
            or row.thread_id in archived_rollout_ids
            or row.thread_id in hidden_rollout_ids
            or _should_hide_generated_workspace(row.cwd)
        ]
        global_state = self._load_global_state()
        known_thread_ids = set(rows) - set(stale_thread_ids)
        known_workspace_roots = {row.cwd for row in rows.values() if row.thread_id in known_thread_ids and row.cwd}
        hidden_state_thread_ids = set(stale_thread_ids) | archived_rollout_ids | hidden_rollout_ids
        updated_global_state, removed_roots = _remove_stale_workspace_roots(
            global_state,
            known_thread_ids=known_thread_ids,
            known_workspace_roots=known_workspace_roots,
            hidden_thread_ids=hidden_state_thread_ids,
        )
        global_state_changed = updated_global_state is not None and updated_global_state != global_state
        workspace_roots_to_hide = sorted(
            {Path(root) for root in [*removed_roots, *_discover_stale_workspace_roots(self.codex_home)] if Path(root).exists()}
        )
        removed_rollout_sessions = sorted(
            [
                rollout
                for rollout in rollouts.values()
                if not rollout.archived and rollout.thread_id not in known_thread_ids and _should_hide_generated_workspace(rollout.cwd)
            ],
            key=lambda rollout: str(rollout.path),
        )
        removed_rollouts = [rollout.path for rollout in removed_rollout_sessions]
        session_index_hidden_ids = (
            set(stale_thread_ids)
            | archived_rollout_ids
            | hidden_rollout_ids
            | {rollout.thread_id for rollout in removed_rollout_sessions}
        )
        removed_session_index_entries = _session_index_entries_to_remove(self.session_index_path, session_index_hidden_ids)
        changed = bool(
            stale_thread_ids
            or global_state_changed
            or workspace_roots_to_hide
            or archived_rollouts
            or removed_rollouts
            or removed_session_index_entries
            or legacy_hidden_archived_rollouts
            or legacy_hidden_removed_rollouts
            or legacy_hidden_workspace_roots
        )
        backup_dir: Path | None = None
        if changed and not dry_run:
            files_to_backup = [
                self.db_path,
                *archived_rollouts,
                *removed_rollouts,
                *legacy_hidden_archived_rollouts,
                *legacy_hidden_removed_rollouts,
            ]
            if self.global_state_path.exists():
                files_to_backup.append(self.global_state_path)
            if self.session_index_path.exists():
                files_to_backup.append(self.session_index_path)
            backup_dir = self.backups.create(files_to_backup)
            self._delete_thread_rows(stale_thread_ids)
            if updated_global_state is not None:
                self.global_state_path.write_text(
                    json.dumps(updated_global_state, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            _remove_session_index_entries(self.session_index_path, session_index_hidden_ids)
            self._hide_archived_rollouts(archived_rollouts)
            self._hide_removed_rollouts(removed_rollouts)
            self._hide_workspace_roots(workspace_roots_to_hide)
            self._move_rollouts(legacy_hidden_archived_rollouts, self.hidden_archived_root)
            self._move_rollouts(legacy_hidden_removed_rollouts, self.hidden_removed_root)
            self._move_paths(legacy_hidden_workspace_roots, self.hidden_workspace_root)
        return HideStaleWorkspacesReport(
            codex_home=str(self.codex_home),
            dry_run=dry_run,
            changed=changed,
            backup_dir=str(backup_dir) if backup_dir else None,
            matched_thread_ids=stale_thread_ids,
            removed_session_index_entries=removed_session_index_entries,
            removed_thread_rows=0 if dry_run else len(stale_thread_ids),
            removed_workspace_roots=removed_roots,
            hidden_workspace_roots=[str(path) for path in workspace_roots_to_hide],
            hidden_archived_rollouts=[str(path) for path in archived_rollouts],
            hidden_removed_rollouts=[str(path) for path in removed_rollouts],
        )

    def _load_rollouts(self) -> dict[str, RolloutSession]:
        rollouts: dict[str, RolloutSession] = {}
        for root, archived in ((self.sessions_root, False), (self.archived_root, True)):
            if not root.exists():
                continue
            for file_path in root.rglob("rollout-*.jsonl"):
                session = self._parse_rollout(file_path, archived=archived)
                if session:
                    rollouts[session.thread_id] = session
        return rollouts

    def _parse_rollout(self, file_path: Path, *, archived: bool) -> RolloutSession | None:
        try:
            text = file_path.read_text(encoding="utf-8")
            first_line = text.splitlines()[0]
            payload = json.loads(first_line)
        except (IndexError, OSError, json.JSONDecodeError):
            return None
        meta = payload.get("payload", {})
        thread_id = meta.get("id")
        if not thread_id:
            return None
        return RolloutSession(
            thread_id=str(thread_id),
            path=file_path,
            archived=archived,
            provider=_optional_str(meta.get("model_provider")),
            cwd=_optional_str(meta.get("cwd")),
            encrypted_content_count=text.count('"encrypted_content"'),
        )

    def _load_thread_rows(self) -> dict[str, ThreadRow]:
        if not self.db_path.exists():
            return {}
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            columns = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(threads)").fetchall()
            }
            optional_columns = [
                column
                for column in ("updated_at_ms", "source", "first_user_message")
                if column in columns
            ]
            selected_columns = ["id", "rollout_path", "model_provider", "cwd", "archived", *optional_columns]
            rows = conn.execute(f"SELECT {', '.join(selected_columns)} FROM threads").fetchall()
        finally:
            conn.close()
        return {
            str(row["id"]): ThreadRow(
                thread_id=str(row["id"]),
                rollout_path=str(row["rollout_path"]),
                provider=str(row["model_provider"]),
                cwd=str(row["cwd"]),
                archived=bool(row["archived"]),
                updated_at_ms=_row_int(row, "updated_at_ms"),
                source=_row_optional_str(row, "source"),
                first_user_message=_row_optional_str(row, "first_user_message"),
            )
            for row in rows
        }

    def _load_global_state(self) -> dict[str, Any] | None:
        if not self.global_state_path.exists():
            return None
        try:
            payload = json.loads(self.global_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) else None

    def _workspace_root_sync_report(
        self,
        rows: Any,
        *,
        dry_run: bool,
    ) -> WorkspaceRootReport:
        roots = _workspace_roots_from_rows(rows)
        global_state = self._load_global_state()
        if not roots:
            return WorkspaceRootReport(
                present=global_state is not None,
                updated=False,
                updated_workspace_roots=0,
                saved_workspace_root_count=0,
                missing_workspace_roots=[],
            )
        if global_state is None:
            global_state = {}
        existing_roots = _global_state_workspace_roots(global_state)
        merged_roots = _merge_roots(existing_roots, roots)
        missing_roots = [root for root in roots if root not in existing_roots]
        updated = bool(missing_roots)
        if updated and not dry_run:
            global_state = dict(global_state)
            global_state["electron-saved-workspace-roots"] = merged_roots
            global_state["active-workspace-roots"] = _merge_roots(
                _list_str(global_state.get("active-workspace-roots")),
                roots,
            )
            global_state["project-order"] = _merge_roots(
                _list_str(global_state.get("project-order")),
                roots,
            )
            self.global_state_path.write_text(
                json.dumps(global_state, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        return WorkspaceRootReport(
            present=True,
            updated=updated,
            updated_workspace_roots=len(missing_roots),
            saved_workspace_root_count=len(merged_roots),
            missing_workspace_roots=missing_roots,
        )

    def _sync_workspace_roots(self, rows: Any) -> WorkspaceRootReport:
        return self._workspace_root_sync_report(rows, dry_run=False)

    def _build_preview_items(
        self,
        *,
        rollouts: dict[str, RolloutSession],
        rows: dict[str, ThreadRow],
        thread_ids: list[str] | None,
        target_provider: str | None,
        target_cwd: str | None,
    ) -> list[SyncPreviewItem]:
        selected_ids = set(thread_ids) if thread_ids else set(rollouts) | set(rows)
        items: list[SyncPreviewItem] = []
        for thread_id in sorted(selected_ids):
            rollout = rollouts.get(thread_id)
            row = rows.get(thread_id)
            desired_provider = target_provider
            desired_cwd = target_cwd
            needs_rollout_update = False
            needs_db_update = False
            if rollout and desired_provider and rollout.provider != desired_provider:
                needs_rollout_update = True
            if rollout and desired_cwd and rollout.cwd != desired_cwd:
                needs_rollout_update = True
            if row:
                actual_rollout_path = str(rollout.path) if rollout else row.rollout_path
                actual_archived = rollout.archived if rollout else row.archived
                if desired_provider and row.provider != desired_provider:
                    needs_db_update = True
                if desired_cwd and row.cwd != desired_cwd:
                    needs_db_update = True
                if row.rollout_path != actual_rollout_path:
                    needs_db_update = True
                if row.archived != actual_archived:
                    needs_db_update = True
            items.append(
                SyncPreviewItem(
                    thread_id=thread_id,
                    rollout_path=str(rollout.path) if rollout else None,
                    db_rollout_path=row.rollout_path if row else None,
                    rollout_provider=rollout.provider if rollout else None,
                    db_provider=row.provider if row else None,
                    target_provider=desired_provider,
                    rollout_cwd=rollout.cwd if rollout else None,
                    db_cwd=row.cwd if row else None,
                    target_cwd=desired_cwd,
                    rollout_archived=rollout.archived if rollout else None,
                    db_archived=row.archived if row else None,
                    missing_rollout=rollout is None,
                    missing_db_row=row is None,
                    needs_rollout_update=needs_rollout_update,
                    needs_db_update=needs_db_update,
                )
            )
        return items

    def _rewrite_rollout(self, file_path: Path, *, provider: str, cwd: str | None) -> None:
        lines = file_path.read_text(encoding="utf-8").splitlines(keepends=True)
        first_payload = json.loads(lines[0])
        meta = dict(first_payload.get("payload", {}))
        meta["model_provider"] = provider
        if cwd is not None:
            meta["cwd"] = cwd
        first_payload["payload"] = meta
        newline = "\n" if lines[0].endswith("\n") else ""
        lines[0] = json.dumps(first_payload, ensure_ascii=False, separators=(",", ":")) + newline
        file_path.write_text("".join(lines), encoding="utf-8")

    def _update_thread_rows(
        self,
        items: list[SyncPreviewItem],
        *,
        target_provider: str,
        target_cwd: str | None,
    ) -> None:
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                for item in items:
                    assignments = [
                        ("model_provider", target_provider),
                        ("archived", 1 if item.rollout_archived else 0),
                    ]
                    if item.rollout_path is not None:
                        assignments.append(("rollout_path", item.rollout_path))
                    if target_cwd is not None:
                        assignments.append(("cwd", target_cwd))
                    clause = ", ".join(f"{column} = ?" for column, _ in assignments)
                    values = [value for _, value in assignments]
                    values.append(item.thread_id)
                    conn.execute(f"UPDATE threads SET {clause} WHERE id = ?", values)
        finally:
            conn.close()

    def _delete_thread_rows(self, thread_ids: list[str]) -> None:
        if not thread_ids:
            return
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                conn.executemany(
                    "DELETE FROM threads WHERE id = ?",
                    [(thread_id,) for thread_id in thread_ids],
                )
        finally:
            conn.close()

    def _hide_archived_rollouts(self, rollout_paths: list[Path]) -> None:
        self._move_rollouts(rollout_paths, self.hidden_archived_root)

    def _hide_removed_rollouts(self, rollout_paths: list[Path]) -> None:
        self._move_rollouts(rollout_paths, self.hidden_removed_root)

    def _hide_workspace_roots(self, workspace_roots: list[Path]) -> None:
        self._move_paths(workspace_roots, self.hidden_workspace_root)

    def _move_rollouts(self, rollout_paths: list[Path], destination_root: Path) -> None:
        self._move_paths(rollout_paths, destination_root)

    def _move_paths(self, paths: list[Path], destination_root: Path) -> None:
        if not paths:
            return
        destination_root.mkdir(parents=True, exist_ok=True)
        seen: set[Path] = set()
        for source in paths:
            if source in seen or not source.exists():
                continue
            seen.add(source)
            destination = destination_root / source.name
            suffix = 0
            while destination.exists():
                suffix += 1
                destination = destination_root / f"{source.stem}-{suffix}{source.suffix}"
            shutil.move(str(source), str(destination))

    def _validate_provider_defined(self, provider: str) -> None:
        if not self.config_path.exists():
            raise RuntimeError("config.toml is required before switching provider")
        data = tomllib.loads(self.config_path.read_text(encoding="utf-8"))
        defined = {str(data.get("model_provider"))} if data.get("model_provider") else set()
        provider_tables = data.get("model_providers")
        if isinstance(provider_tables, dict):
            defined.update(str(key) for key in provider_tables)
        if provider not in defined:
            available = ", ".join(sorted(defined)) or "none"
            raise RuntimeError(f"provider '{provider}' is not defined in config.toml; available providers: {available}")

    def _config_needs_update(
        self,
        *,
        provider: str,
        model: str | None,
        require_provider_table: bool = False,
    ) -> bool:
        if not self.config_path.exists():
            return True
        text = self.config_path.read_text(encoding="utf-8")
        current_provider = _match_top_level_value(text, "model_provider")
        current_model = _match_top_level_value(text, "model")
        if current_provider != provider:
            return True
        if model is not None and current_model != model:
            return True
        if require_provider_table and _provider_table_range(text, provider) is None:
            return True
        return False

    def _rewrite_config(
        self,
        *,
        provider: str,
        model: str | None,
        copy_current_provider_config: bool = False,
    ) -> None:
        text = self.config_path.read_text(encoding="utf-8") if self.config_path.exists() else ""
        current_provider = _match_top_level_value(text, "model_provider")
        if copy_current_provider_config and current_provider:
            source = current_provider if _provider_table_range(text, current_provider) else _first_provider_table_name(text)
            if source:
                text = _copy_provider_table_if_missing(text, source=source, target=provider)
        text = _replace_or_insert_top_level_value(text, "model_provider", provider)
        if model is not None:
            text = _replace_or_insert_top_level_value(text, "model", model)
        self.config_path.write_text(text, encoding="utf-8")


def _match_top_level_value(text: str, key: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("["):
            if stripped.startswith("["):
                break
            continue
        match = re.match(rf"^{re.escape(key)}\s*=\s*\"([^\"]*)\"\s*$", line)
        if match:
            return match.group(1)
    return None


def _replace_or_insert_top_level_value(text: str, key: str, value: str) -> str:
    lines = text.splitlines()
    inserted = False
    replaced = False
    output: list[str] = []
    for line in lines:
        if not replaced and line.startswith("["):
            output.append(f'{key} = "{value}"')
            inserted = True
            replaced = True
        match = re.match(rf"^{re.escape(key)}\s*=\s*\"([^\"]*)\"\s*$", line)
        if not replaced and match:
            output.append(f'{key} = "{value}"')
            replaced = True
            continue
        output.append(line)
    if not replaced:
        output.append(f'{key} = "{value}"')
    result = "\n".join(output)
    if text.endswith("\n") or not result.endswith("\n"):
        result += "\n"
    return result


def _copy_provider_table_if_missing(text: str, *, source: str, target: str) -> str:
    if source == target or _provider_table_range(text, target):
        return text
    source_range = _provider_table_range(text, source)
    if not source_range:
        return text
    lines = text.splitlines()
    start, end = source_range
    copied = [
        _rewrite_copied_provider_table_line(line, source=source, target=target)
        for line in lines[start:end]
    ]
    if copied == lines[start:end]:
        return text
    insertion = ["", *copied]
    output = [*lines[:end], *insertion, *lines[end:]]
    result = "\n".join(output)
    if text.endswith("\n") or not result.endswith("\n"):
        result += "\n"
    return result


def _provider_table_range(text: str, provider: str) -> tuple[int, int] | None:
    lines = text.splitlines()
    header_pattern = re.compile(rf"^\[model_providers\.{re.escape(provider)}\]$")
    start = None
    for index, line in enumerate(lines):
        if header_pattern.match(line.strip()):
            start = index
            break
    if start is None:
        return None
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = index
            break
    return start, end


def _first_provider_table_name(text: str) -> str | None:
    for line in text.splitlines():
        match = re.match(r"^\[model_providers\.([^\]]+)\]$", line.strip())
        if match:
            return match.group(1)
    return None


def _rewrite_copied_provider_table_line(line: str, *, source: str, target: str) -> str:
    line = re.sub(rf"^\[model_providers\.{re.escape(source)}\]$", f"[model_providers.{target}]", line)
    if re.match(r"^name\s*=\s*\"[^\"]*\"\s*$", line):
        return f'name = "{target}"'
    return line


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _row_optional_str(row: sqlite3.Row, key: str) -> str | None:
    try:
        return _optional_str(row[key])
    except (IndexError, KeyError):
        return None


def _row_int(row: sqlite3.Row, key: str) -> int:
    try:
        value = row[key]
    except (IndexError, KeyError):
        return 0
    return int(value or 0)


def _list_str(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _workspace_roots_from_rows(rows: Any) -> list[str]:
    roots: list[str] = []
    seen: set[str] = set()
    for row in sorted(rows, key=lambda item: item.updated_at_ms, reverse=True):
        if not row.cwd or row.cwd in seen:
            continue
        seen.add(row.cwd)
        roots.append(row.cwd)
    return roots


def _global_state_workspace_roots(global_state: dict[str, Any] | None) -> list[str]:
    if not global_state:
        return []
    roots = _list_str(global_state.get("electron-saved-workspace-roots"))
    if roots:
        return roots
    return _list_str(global_state.get("project-order"))


def _missing_workspace_roots(global_state: dict[str, Any] | None, roots: list[str]) -> list[str]:
    existing = set(_global_state_workspace_roots(global_state))
    return [root for root in roots if root not in existing]


def _merge_roots(existing: list[str], additions: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for root in [*additions, *existing]:
        if not root or root in seen:
            continue
        seen.add(root)
        merged.append(root)
    return merged


def _is_generated_codex_workspace(root: str | None) -> bool:
    if not root:
        return False
    parts = Path(root).parts
    for index, part in enumerate(parts[:-2]):
        if part == "Documents" and parts[index + 1] == "Codex":
            return bool(re.match(r"^\d{4}-\d{2}-\d{2}(?:-|$)", parts[index + 2]))
    return False


def _is_generated_codex_project_root(root: str | None) -> bool:
    if not root:
        return False
    path = Path(root)
    parts = path.parts
    for index, part in enumerate(parts[:-2]):
        if part != "Documents" or parts[index + 1] != "Codex":
            continue
        project_part = parts[index + 2]
        if re.match(r"^\d{4}-\d{2}-\d{2}-", project_part):
            return True
        if re.match(r"^\d{4}-\d{2}-\d{2}$", project_part) and len(parts) > index + 3:
            return True
    return False


def _should_hide_generated_workspace(root: str | None) -> bool:
    if not _is_generated_codex_project_root(root):
        return False
    return True


def _discover_stale_workspace_roots(codex_home: Path) -> list[str]:
    documents_codex = codex_home.parent / "Documents" / "Codex"
    if not documents_codex.exists():
        return []
    roots: list[str] = []
    for child in documents_codex.iterdir():
        if not child.is_dir():
            continue
        if _should_hide_generated_workspace(str(child)):
            roots.append(str(child))
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", child.name):
            continue
        for grandchild in child.iterdir():
            if grandchild.is_dir() and _should_hide_generated_workspace(str(grandchild)):
                roots.append(str(grandchild))
    return roots


def _remove_stale_workspace_roots(
    global_state: dict[str, Any] | None,
    *,
    known_thread_ids: set[str] | None = None,
    known_workspace_roots: set[str] | None = None,
    hidden_thread_ids: set[str] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    if global_state is None:
        return None, []
    next_state = dict(global_state)
    removed: list[str] = []
    seen_removed: set[str] = set()
    thread_ids = known_thread_ids or set()
    visible_roots = known_workspace_roots or set()

    def mark_removed(root: str) -> None:
        if root not in seen_removed:
            removed.append(root)
            seen_removed.add(root)

    def clean_workspace_state(state: dict[str, Any]) -> None:
        for key in ("electron-saved-workspace-roots", "active-workspace-roots", "project-order"):
            roots = _list_str(state.get(key))
            if not roots:
                continue
            kept = []
            for root in roots:
                if _should_hide_generated_workspace(root) or (
                    visible_roots and _is_generated_codex_workspace(root) and root not in visible_roots
                ):
                    mark_removed(root)
                    continue
                kept.append(root)
            state[key] = kept
        value = state.get("sidebar-collapsed-groups")
        if not isinstance(value, dict):
            return
        kept = {}
        for root, collapsed in value.items():
            root_text = str(root)
            if _should_hide_generated_workspace(root_text) or (
                visible_roots and _is_generated_codex_workspace(root_text) and root_text not in visible_roots
            ):
                mark_removed(root_text)
                continue
            kept[root_text] = collapsed
        state["sidebar-collapsed-groups"] = kept

    clean_workspace_state(next_state)
    if known_thread_ids is not None:
        projectless_ids = _list_str(next_state.get("projectless-thread-ids"))
        if projectless_ids:
            next_state["projectless-thread-ids"] = [thread_id for thread_id in projectless_ids if thread_id in thread_ids]
        hints = next_state.get("thread-workspace-root-hints")
        if isinstance(hints, dict):
            next_state["thread-workspace-root-hints"] = {
                str(thread_id): root
                for thread_id, root in hints.items()
                if str(thread_id) in thread_ids
            }
    atom_state = next_state.get("electron-persisted-atom-state")
    if isinstance(atom_state, dict):
        next_atom_state = dict(atom_state)
        clean_workspace_state(next_atom_state)
        next_state["electron-persisted-atom-state"] = next_atom_state
    if hidden_thread_ids:
        next_state = _remove_hidden_thread_state(next_state, hidden_thread_ids)
    return next_state, removed


def _remove_hidden_thread_state(value: Any, hidden_thread_ids: set[str]) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _remove_hidden_thread_state(child, hidden_thread_ids)
            for key, child in value.items()
            if str(key) not in hidden_thread_ids
        }
    if isinstance(value, list):
        return [
            _remove_hidden_thread_state(item, hidden_thread_ids)
            for item in value
            if not (isinstance(item, str) and item in hidden_thread_ids)
        ]
    return value


def _rollout_thread_ids(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return _rollout_thread_ids_from_paths(root.glob("rollout-*.jsonl"))


def _rollout_thread_ids_from_paths(paths: Any) -> set[str]:
    thread_ids: set[str] = set()
    for path in paths:
        match = re.search(r"-(019[0-9a-f-]{33})\.jsonl$", Path(path).name)
        if match:
            thread_ids.add(match.group(1))
    return thread_ids


def _session_index_entries_to_remove(path: Path, hidden_thread_ids: set[str]) -> list[str]:
    if not hidden_thread_ids or not path.exists():
        return []
    removed: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        thread_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(thread_id, str) and thread_id in hidden_thread_ids:
            removed.append(thread_id)
    return removed


def _remove_session_index_entries(path: Path, hidden_thread_ids: set[str]) -> None:
    if not hidden_thread_ids or not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    kept: list[str] = []
    changed = False
    for line in lines:
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            kept.append(line)
            continue
        thread_id = payload.get("id") if isinstance(payload, dict) else None
        if isinstance(thread_id, str) and thread_id in hidden_thread_ids:
            changed = True
            continue
        kept.append(line)
    if changed:
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")


def _project_visibility(rows: Any, *, limit: int) -> list[dict[str, Any]]:
    by_cwd: dict[str, dict[str, Any]] = {}
    sorted_rows = sorted(rows, key=lambda row: row.updated_at_ms, reverse=True)
    for rank, row in enumerate(sorted_rows, start=1):
        if not row.cwd:
            continue
        current = by_cwd.get(row.cwd)
        if current is None:
            by_cwd[row.cwd] = {
                "cwd": row.cwd,
                "first_rank": rank,
                "thread_count": 1,
                "visible_in_first_50": rank <= 50,
                "latest_thread_id": row.thread_id,
                "latest_provider": row.provider,
                "latest_source": row.source,
                "latest_first_user_message": row.first_user_message,
            }
            continue
        current["thread_count"] += 1
    return list(by_cwd.values())[:limit]


def _encrypted_content_summary(rollouts: Any, *, limit: int) -> dict[str, Any]:
    rollouts_with_encrypted_content = [
        rollout for rollout in rollouts if rollout.encrypted_content_count > 0
    ]
    return {
        "threads": len(rollouts_with_encrypted_content),
        "items": sum(rollout.encrypted_content_count for rollout in rollouts_with_encrypted_content),
        "preview": [
            {
                "thread_id": rollout.thread_id,
                "rollout_path": str(rollout.path),
                "encrypted_content_count": rollout.encrypted_content_count,
            }
            for rollout in rollouts_with_encrypted_content[:limit]
        ],
    }


def _is_rollout_locked(file_path: Path) -> bool:
    return file_path.with_suffix(file_path.suffix + ".lock").exists()


def _locked_rollout_files(rollouts: Any) -> list[Path]:
    return [rollout.path for rollout in rollouts if _is_rollout_locked(rollout.path)]


def _file_mtime_ms(path: Path) -> int:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return 0


def _preview_item_to_dict(item: SyncPreviewItem) -> dict[str, Any]:
    return {
        "thread_id": item.thread_id,
        "rollout_path": item.rollout_path,
        "db_rollout_path": item.db_rollout_path,
        "rollout_provider": item.rollout_provider,
        "db_provider": item.db_provider,
        "target_provider": item.target_provider,
        "rollout_cwd": item.rollout_cwd,
        "db_cwd": item.db_cwd,
        "target_cwd": item.target_cwd,
        "rollout_archived": item.rollout_archived,
        "db_archived": item.db_archived,
        "missing_rollout": item.missing_rollout,
        "missing_db_row": item.missing_db_row,
        "needs_rollout_update": item.needs_rollout_update,
        "needs_db_update": item.needs_db_update,
    }


def _backup_companion_paths(file_path: Path) -> list[Path]:
    candidates = [file_path]
    if file_path.name.endswith(".sqlite"):
        candidates.append(Path(f"{file_path}-wal"))
        candidates.append(Path(f"{file_path}-shm"))
    return candidates


def _restore_category(relative: str) -> str:
    if relative == "config.toml":
        return "config"
    if relative == ".codex-global-state.json":
        return "global_state"
    if relative.startswith("sessions/") or relative.startswith("archived_sessions/"):
        return "sessions"
    if relative.startswith("state_5.sqlite"):
        return "database"
    return "other"


def _dedupe_paths(paths: list[Path]) -> list[Path]:
    result: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        result.append(path)
    return result
