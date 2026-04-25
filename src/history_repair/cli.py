from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from .host import DesktopSessionHost
from .models import ProviderCapabilities, RouteTarget
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


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
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
