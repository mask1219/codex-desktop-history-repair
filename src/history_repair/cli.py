from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from typing import Any

from .autosync_agent import AutosyncLaunchAgent
from .host import DesktopSessionHost
from .models import ProviderCapabilities, RouteTarget
from .provider_sync import CodexProviderSyncService
from .providers import ResponsesApiProviderClient


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="history-repair")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init_parser = subparsers.add_parser("init")
    init_parser.add_argument("--db", required=True, help="SQLite database path")

    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--db", required=True, help="SQLite database path")
    recover_parser.add_argument(
        "--pending-timeout-ms",
        type=int,
        default=60_000,
        help="Pending assistant timeout in milliseconds",
    )

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--db", required=True, help="SQLite database path")
    export_parser.add_argument("--output", required=True, help="Output JSONL path")
    export_parser.add_argument(
        "--thread-id",
        action="append",
        default=None,
        help="Optional thread id to export, repeatable",
    )

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--db", required=True, help="SQLite database path")
    import_parser.add_argument("--input", required=True, help="Input JSONL path")

    list_parser = subparsers.add_parser("list")
    list_parser.add_argument("--db", required=True, help="SQLite database path")
    list_parser.add_argument(
        "--status",
        default=None,
        help="Optional thread status filter, e.g. active",
    )

    show_parser = subparsers.add_parser("show")
    show_parser.add_argument("--db", required=True, help="SQLite database path")
    show_parser.add_argument("--thread-id", required=True, help="Thread id")

    send_parser = subparsers.add_parser("send")
    send_parser.add_argument("--db", required=True, help="SQLite database path")
    send_parser.add_argument("--thread-id", required=True, help="Thread id")
    send_parser.add_argument("--message", required=True, help="User message content")
    send_parser.add_argument("--provider", required=True, help="Provider name")
    send_parser.add_argument("--account-id", required=True, help="Account id")
    send_parser.add_argument("--model", required=True, help="Model id")
    send_parser.add_argument("--base-url", required=True, help="Responses API base url")
    send_parser.add_argument("--api-key", default=None, help="Responses API key")
    send_parser.add_argument(
        "--api-key-env",
        default="HISTORY_REPAIR_API_KEY",
        help="Environment variable name for API key fallback",
    )
    send_parser.add_argument("--thread-title", default=None, help="Optional thread title")
    send_parser.add_argument("--instructions", default=None, help="Optional instructions")
    send_parser.add_argument(
        "--max-context-tokens",
        type=int,
        default=120_000,
        help="Context window budget used by continuation planner",
    )
    send_parser.add_argument(
        "--supports-previous-response-id",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether provider supports previous_response_id",
    )
    send_parser.add_argument(
        "--timeout-sec",
        type=float,
        default=120.0,
        help="HTTP timeout in seconds",
    )

    status_parser = subparsers.add_parser("provider-status")
    status_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    status_parser.add_argument("--provider", default=None, help="Target provider override")
    status_parser.add_argument(
        "--thread-id",
        action="append",
        default=None,
        help="Optional thread id filter, repeatable",
    )
    status_parser.add_argument("--limit", type=int, default=20, help="Preview item limit")

    sync_parser = subparsers.add_parser("provider-sync")
    sync_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    sync_parser.add_argument("--provider", default=None, help="Target provider override")
    sync_parser.add_argument("--cwd", default=None, help="Optional target cwd override")
    sync_parser.add_argument(
        "--thread-id",
        action="append",
        default=None,
        help="Optional thread id filter, repeatable",
    )
    sync_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preview changes without writing files",
    )

    switch_parser = subparsers.add_parser("provider-switch")
    switch_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    switch_parser.add_argument("--provider", required=True, help="Target provider")
    switch_parser.add_argument("--model", default=None, help="Optional target model")
    switch_parser.add_argument("--cwd", default=None, help="Optional target cwd override")
    switch_parser.add_argument(
        "--thread-id",
        action="append",
        default=None,
        help="Optional thread id filter, repeatable",
    )
    switch_parser.add_argument(
        "--sync-history",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to sync rollout files and state db after updating config",
    )
    switch_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Preview changes without writing files",
    )

    restore_parser = subparsers.add_parser("provider-restore")
    restore_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    restore_parser.add_argument("--backup-dir", required=True, help="Backup directory to restore")
    restore_parser.add_argument(
        "--config",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to restore config.toml",
    )
    restore_parser.add_argument(
        "--db",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to restore state_5.sqlite and companions",
    )
    restore_parser.add_argument(
        "--sessions",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to restore rollout files",
    )
    restore_parser.add_argument(
        "--global-state",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Whether to restore .codex-global-state.json",
    )

    prune_parser = subparsers.add_parser("provider-prune-backups")
    prune_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    prune_parser.add_argument("--keep", type=int, default=10, help="Number of backups to keep")

    hide_stale_parser = subparsers.add_parser("provider-hide-stale-workspaces")
    hide_stale_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    hide_stale_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preview stale generated Codex workspace cleanup without writing files",
    )

    electron_state_parser = subparsers.add_parser("provider-clear-electron-state")
    electron_state_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    electron_state_parser.add_argument(
        "--app-support-root",
        default=None,
        help="Codex app support directory, defaults to ~/Library/Application Support/Codex",
    )
    electron_state_parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Preview Electron persisted state cleanup without writing files",
    )

    autosync_parser = subparsers.add_parser("provider-autosync")
    autosync_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    autosync_parser.add_argument("--provider", default=None, help="Optional target provider override")
    autosync_parser.add_argument(
        "--interval-sec",
        type=float,
        default=5.0,
        help="Seconds between config/provider checks",
    )
    autosync_parser.add_argument(
        "--once",
        action="store_true",
        help="Run one check and exit",
    )
    autosync_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Preview item limit",
    )
    autosync_parser.add_argument(
        "--quiet",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Only print when a sync happens",
    )
    autosync_parser.add_argument(
        "--switch-provider",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether to rewrite config.toml to the target provider before syncing history",
    )
    autosync_parser.add_argument(
        "--cleanup-stale-workspaces",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether autosync may run stale workspace cleanup with write operations",
    )

    install_autosync_parser = subparsers.add_parser("provider-autosync-install")
    install_autosync_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    install_autosync_parser.add_argument("--provider", default=None, help="Optional target provider override")
    install_autosync_parser.add_argument(
        "--interval-sec",
        type=float,
        default=5.0,
        help="Seconds between config/provider checks",
    )
    install_autosync_parser.add_argument(
        "--switch-provider",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether the background agent may rewrite config.toml to the inferred/provider target",
    )
    install_autosync_parser.add_argument(
        "--cleanup-stale-workspaces",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Whether the background agent may run stale workspace cleanup with write operations",
    )
    install_autosync_parser.add_argument(
        "--load",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Load the LaunchAgent immediately after writing it",
    )

    status_autosync_parser = subparsers.add_parser("provider-autosync-status")
    status_autosync_parser.add_argument("--codex-home", default=None, help="Codex home directory")

    uninstall_autosync_parser = subparsers.add_parser("provider-autosync-uninstall")
    uninstall_autosync_parser.add_argument("--codex-home", default=None, help="Codex home directory")
    uninstall_autosync_parser.add_argument(
        "--unload",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Unload the LaunchAgent before removing it",
    )

    return parser.parse_args(argv)


def _print_json(data: dict[str, Any]) -> None:
    print(json.dumps(data, ensure_ascii=False))


def _with_runtime_meta(payload: dict[str, Any], runtime_meta: dict[str, Any]) -> dict[str, Any]:
    merged = dict(payload)
    merged.update(runtime_meta)
    return merged


def _resolve_api_key(args: argparse.Namespace) -> str | None:
    if args.api_key:
        return str(args.api_key)
    env_name = str(args.api_key_env)
    value = os.environ.get(env_name)
    if value:
        return value
    return None


def _provider_status_needs_sync(report: Any) -> bool:
    return bool(
        report.current_provider
        and (
            report.provider_mismatch_count
            or report.path_mismatch_count
            or report.archived_mismatch_count
            or report.missing_workspace_roots
        )
    )


def _autosync_agent_payload(report: Any) -> dict[str, Any]:
    return {
        "label": report.label,
        "plist_path": report.plist_path,
        "log_dir": report.log_dir,
        "installed": report.installed,
        "loaded": report.loaded,
        "action": report.action,
        "program_arguments": report.command,
        "message": report.message,
    }


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.command in {
        "provider-autosync-install",
        "provider-autosync-status",
        "provider-autosync-uninstall",
    }:
        agent = AutosyncLaunchAgent(codex_home=getattr(args, "codex_home", None))
        if args.command == "provider-autosync-install":
            report = agent.install(
                interval_sec=float(args.interval_sec),
                switch_provider=bool(args.switch_provider),
                cleanup_stale_workspaces=bool(args.cleanup_stale_workspaces),
                provider=str(args.provider) if args.provider else None,
                load=bool(args.load),
            )
        elif args.command == "provider-autosync-uninstall":
            report = agent.uninstall(unload=bool(args.unload))
        else:
            report = agent.status()
        _print_json({"status": "ok", "command": args.command, **_autosync_agent_payload(report)})
        return 0

    if args.command.startswith("provider-"):
        service = CodexProviderSyncService(codex_home=getattr(args, "codex_home", None))
        if args.command == "provider-status":
            report = service.status(
                thread_ids=args.thread_id,
                target_provider=str(args.provider) if args.provider else None,
                limit=int(args.limit),
            )
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-sync":
            report = service.sync(
                target_provider=str(args.provider) if args.provider else None,
                target_cwd=str(args.cwd) if args.cwd else None,
                thread_ids=args.thread_id,
                dry_run=bool(args.dry_run),
            )
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-switch":
            report = service.switch_provider(
                provider=str(args.provider),
                model=str(args.model) if args.model else None,
                target_cwd=str(args.cwd) if args.cwd else None,
                thread_ids=args.thread_id,
                sync_history=bool(args.sync_history),
                dry_run=bool(args.dry_run),
            )
            payload = {
                "status": "ok",
                "command": args.command,
                "codex_home": report.codex_home,
                "provider": report.provider,
                "model": report.model,
                "dry_run": report.dry_run,
                "config_updated": report.config_updated,
                "backup_dir": report.backup_dir,
                "sync": report.sync.__dict__ if report.sync else None,
            }
            _print_json(payload)
            return 0

        if args.command == "provider-restore":
            report = service.restore(
                args.backup_dir,
                include_config=bool(args.config),
                include_database=bool(args.db),
                include_sessions=bool(args.sessions),
                include_global_state=bool(args.global_state),
            )
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-prune-backups":
            report = service.prune_backups(keep=int(args.keep))
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-hide-stale-workspaces":
            report = service.hide_stale_workspaces(dry_run=bool(args.dry_run))
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-clear-electron-state":
            report = service.clear_electron_persisted_state(
                dry_run=bool(args.dry_run),
                app_support_root=str(args.app_support_root) if args.app_support_root else None,
            )
            _print_json({"status": "ok", "command": args.command, **report.__dict__})
            return 0

        if args.command == "provider-autosync":
            interval_sec = max(float(args.interval_sec), 0.1)
            try:
                while True:
                    cleanup_stale_workspaces = bool(args.cleanup_stale_workspaces)
                    hide_report = service.hide_stale_workspaces(
                        dry_run=not cleanup_stale_workspaces,
                    )
                    status_report = service.status(limit=int(args.limit))
                    target_provider = str(args.provider) if args.provider else status_report.current_provider
                    inferred_provider = None
                    if bool(args.switch_provider) and not args.provider:
                        inferred_provider = service.latest_history_provider()
                        target_provider = inferred_provider or status_report.current_provider
                    if bool(args.switch_provider) and not target_provider:
                        raise RuntimeError("target provider could not be inferred")
                    provider_config_missing = bool(
                        args.switch_provider
                        and inferred_provider
                        and target_provider
                        and service.provider_table_missing(target_provider)
                    )
                    if bool(args.switch_provider) and target_provider and (
                        status_report.current_provider != target_provider or provider_config_missing
                    ):
                        switch_report = service.switch_provider(
                            provider=target_provider,
                            sync_history=True,
                            allow_implicit_provider=bool(inferred_provider and not args.provider),
                        )
                        _print_json(
                            {
                                "status": "ok",
                                "command": args.command,
                                "event": "switch",
                                "current_provider": status_report.current_provider,
                                "target_provider": target_provider,
                                "inferred_provider": inferred_provider,
                                "config_updated": switch_report.config_updated,
                                "backup_dir": switch_report.backup_dir,
                                "sync": switch_report.sync.__dict__ if switch_report.sync else None,
                            }
                        )
                    elif _provider_status_needs_sync(status_report):
                        sync_report = service.sync(target_provider=target_provider)
                        _print_json(
                            {
                                "status": "ok",
                                "command": args.command,
                                "event": "sync",
                                **sync_report.__dict__,
                            }
                        )
                    elif cleanup_stale_workspaces and hide_report.changed:
                        _print_json(
                            {
                                "status": "ok",
                                "command": args.command,
                                "event": "hide-stale-workspaces",
                                **hide_report.__dict__,
                            }
                        )
                    elif not bool(args.quiet):
                        _print_json(
                            {
                                "status": "ok",
                                "command": args.command,
                                "event": "idle",
                                "codex_home": status_report.codex_home,
                                "current_provider": status_report.current_provider,
                                "target_provider": target_provider,
                                "inferred_provider": inferred_provider,
                                "provider_mismatch_count": status_report.provider_mismatch_count,
                                "missing_workspace_roots": status_report.missing_workspace_roots,
                            }
                        )
                    if bool(args.once):
                        return 0
                    time.sleep(interval_sec)
            except KeyboardInterrupt:
                return 0

        raise RuntimeError(f"Unsupported command: {args.command}")

    host = DesktopSessionHost(Path(args.db))
    try:
        startup_result = host.startup()
        runtime_meta: dict[str, Any] = {"read_only_mode": bool(startup_result.read_only_mode)}
        if startup_result.migration_backup_path:
            runtime_meta["migration_backup_path"] = startup_result.migration_backup_path
        if startup_result.migration_warning:
            runtime_meta["migration_warning"] = startup_result.migration_warning
        runtime_meta["startup_recover_executed"] = bool(startup_result.recover_executed)

        if startup_result.read_only_mode and args.command in {"init", "recover", "import", "send"}:
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "error",
                        "command": str(args.command),
                        "db": str(Path(args.db)),
                        "error": (
                            "database is in read-only recovery mode due to migration failure; "
                            "write operations are disabled"
                        ),
                    },
                    runtime_meta,
                )
            )
            return 1

        if args.command == "init":
            row = host.db.query_one("SELECT value FROM app_meta WHERE key = 'schema_version'")
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "init",
                        "db": str(Path(args.db)),
                        "schema_version": row["value"] if row else None,
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "recover":
            host.service.recover_incomplete_messages(pending_timeout_ms=int(args.pending_timeout_ms))
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "recover",
                        "db": str(Path(args.db)),
                        "pending_timeout_ms": int(args.pending_timeout_ms),
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "export":
            count = host.export_jsonl(output_path=Path(args.output), thread_ids=args.thread_id)
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "export",
                        "db": str(Path(args.db)),
                        "output": str(Path(args.output)),
                        "records_written": count,
                        "thread_ids": args.thread_id or [],
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "import":
            report = host.import_jsonl(Path(args.input))
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "import",
                        "db": str(Path(args.db)),
                        "input": str(Path(args.input)),
                        "imported_threads": report.imported_threads,
                        "imported_messages": report.imported_messages,
                        "imported_routes": report.imported_routes,
                        "imported_summaries": report.imported_summaries,
                        "thread_id_map": report.thread_id_map,
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "list":
            threads = host.list_threads(status=str(args.status) if args.status else None)
            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "list",
                        "db": str(Path(args.db)),
                        "threads": threads,
                        "count": len(threads),
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "show":
            detail = host.get_thread_detail(str(args.thread_id))
            if detail is None:
                _print_json(
                    _with_runtime_meta(
                        {
                            "status": "error",
                            "command": "show",
                            "db": str(Path(args.db)),
                            "thread_id": str(args.thread_id),
                            "error": "thread not found",
                        },
                        runtime_meta,
                    )
                )
                return 1

            _print_json(
                _with_runtime_meta(
                    {
                        "status": "ok",
                        "command": "show",
                        "db": str(Path(args.db)),
                        "thread": detail.thread,
                        "messages": detail.messages,
                        "routes": detail.routes,
                        "summaries": detail.summaries,
                    },
                    runtime_meta,
                )
            )
            return 0

        if args.command == "send":
            api_key = _resolve_api_key(args)
            if not api_key:
                _print_json(
                    _with_runtime_meta(
                        {
                            "status": "error",
                            "command": "send",
                            "db": str(Path(args.db)),
                            "thread_id": str(args.thread_id),
                            "error": (
                                "missing api key: provide --api-key or set "
                                f"env {str(args.api_key_env)}"
                            ),
                        },
                        runtime_meta,
                    )
                )
                return 1

            provider_client = ResponsesApiProviderClient(
                base_url=str(args.base_url),
                api_key=api_key,
                timeout_sec=float(args.timeout_sec),
            )
            host_result = host.send_message(
                thread_id=str(args.thread_id),
                user_content=str(args.message),
                route_target=RouteTarget(
                    provider=str(args.provider),
                    account_id=str(args.account_id),
                    model=str(args.model),
                ),
                capabilities=ProviderCapabilities(
                    supports_previous_response_id=bool(args.supports_previous_response_id),
                    max_context_tokens=int(args.max_context_tokens),
                ),
                provider_client=provider_client,
                thread_title=str(args.thread_title) if args.thread_title else None,
                instructions=str(args.instructions) if args.instructions else None,
            )
            result = host_result.send_result
            status = "ok" if result.success else "error"
            _print_json(
                _with_runtime_meta(
                    {
                        "status": status,
                        "command": "send",
                        "db": str(Path(args.db)),
                        "thread_id": result.thread_id,
                        "user_message_id": result.user_message_id,
                        "assistant_message_id": result.assistant_message_id,
                        "success": result.success,
                        "continuation_mode": result.continuation_mode.value,
                        "remote_response_id": result.remote_response_id,
                        "summary_id": result.summary_id,
                        "error": result.error,
                        "error_code": result.error_code,
                        "continuation_note": result.continuation_note,
                        "ui_notice": host_result.ui_notice,
                    },
                    runtime_meta,
                )
            )
            return 0 if result.success else 1

        raise RuntimeError(f"Unsupported command: {args.command}")
    finally:
        host.close()
