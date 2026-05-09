from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from history_repair import CodexProviderSyncService  # noqa: E402


def _write_rollout(path: Path, *, thread_id: str, provider: str, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    first = {
        "timestamp": "2026-05-08T10:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "id": thread_id,
            "timestamp": "2026-05-08T10:00:00.000Z",
            "cwd": cwd,
            "originator": "Codex Desktop",
            "cli_version": "0.1.0",
            "source": "vscode",
            "model_provider": provider,
        },
    }
    path.write_text(json.dumps(first, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_rollout_with_encrypted_content(path: Path, *, thread_id: str, provider: str, cwd: str) -> None:
    _write_rollout(path, thread_id=thread_id, provider=provider, cwd=cwd)
    encrypted_item = {
        "timestamp": "2026-05-08T10:01:00.000Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "encrypted_content": "opaque"}],
        },
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(encrypted_item, ensure_ascii=False) + "\n")


def _write_config(path: Path, *, current: str = "openai", providers: list[str] | None = None) -> None:
    provider_names = providers or [current]
    lines = [f'model_provider = "{current}"', ""]
    for provider in provider_names:
        lines.extend([f'[model_providers.{provider}]', 'name = "test"', ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _init_db(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE threads (
            id TEXT PRIMARY KEY,
            rollout_path TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            updated_at INTEGER NOT NULL,
            source TEXT NOT NULL,
            model_provider TEXT NOT NULL,
            cwd TEXT NOT NULL,
            title TEXT NOT NULL,
            sandbox_policy TEXT NOT NULL,
            approval_mode TEXT NOT NULL,
            tokens_used INTEGER NOT NULL DEFAULT 0,
            has_user_event INTEGER NOT NULL DEFAULT 0,
            archived INTEGER NOT NULL DEFAULT 0,
            archived_at INTEGER,
            git_sha TEXT,
            git_branch TEXT,
            git_origin_url TEXT,
            cli_version TEXT NOT NULL DEFAULT '',
            first_user_message TEXT NOT NULL DEFAULT '',
            agent_nickname TEXT,
            agent_role TEXT,
            memory_mode TEXT NOT NULL DEFAULT 'enabled',
            model TEXT,
            reasoning_effort TEXT,
            agent_path TEXT,
            created_at_ms INTEGER,
            updated_at_ms INTEGER,
            thread_source TEXT
        );
        """
    )
    conn.commit()
    conn.close()


def _insert_thread(
    db_path: Path,
    *,
    thread_id: str,
    rollout_path: str,
    provider: str,
    cwd: str,
    archived: bool,
    updated_at_ms: int = 1000,
) -> None:
    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute(
            """
            INSERT INTO threads (
                id, rollout_path, created_at, updated_at, source, model_provider, cwd, title,
                sandbox_policy, approval_mode, archived, cli_version, first_user_message,
                memory_mode, created_at_ms, updated_at_ms
            ) VALUES (?, ?, 1, 1, 'vscode', ?, ?, 'Thread', 'workspace-write', 'on-request', ?, '', '', 'enabled', ?, ?)
            """,
            (thread_id, rollout_path, provider, cwd, 1 if archived else 0, updated_at_ms, updated_at_ms),
        )
    conn.close()


class ProviderSyncTests(unittest.TestCase):
    def test_status_provider_source_prefers_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            _write_config(codex_home / "config.toml", current="sub2api", providers=["sub2api"])
            service = CodexProviderSyncService(codex_home)
            report = service.status()
            self.assertEqual(report.current_provider, "sub2api")
            self.assertEqual(report.current_provider_source, "config.toml")

    def test_status_provider_source_uses_auth_mode_chatgpt(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            rollout_path = codex_home / "sessions" / "2026" / "05" / "09" / "rollout-thread-auth.jsonl"
            _init_db(db_path)
            _write_rollout(
                rollout_path,
                thread_id="thread-auth",
                provider="openai",
                cwd="/tmp/project-auth",
            )
            _insert_thread(
                db_path,
                thread_id="thread-auth",
                rollout_path=str(rollout_path),
                provider="openai",
                cwd="/tmp/project-auth",
                archived=False,
            )
            (codex_home / "config.toml").write_text(
                '[model_providers.openai]\nname = "openai"\n',
                encoding="utf-8",
            )
            (codex_home / "auth.json").write_text(
                json.dumps({"auth_mode": "chatgpt"}),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            report = service.status()
            self.assertEqual(report.current_provider, "openai")
            self.assertEqual(report.current_provider_source, "auth.json")

    def test_status_provider_source_uses_recent_thread_when_config_has_no_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            old_rollout = codex_home / "sessions" / "2026" / "05" / "09" / "rollout-thread-old-source.jsonl"
            new_rollout = codex_home / "sessions" / "2026" / "05" / "09" / "rollout-thread-new-source.jsonl"
            _init_db(db_path)
            _write_rollout(
                old_rollout,
                thread_id="thread-old-source",
                provider="openai",
                cwd="/tmp/project-source",
            )
            _insert_thread(
                db_path,
                thread_id="thread-old-source",
                rollout_path=str(old_rollout),
                provider="openai",
                cwd="/tmp/project-source",
                archived=False,
                updated_at_ms=1000,
            )
            _write_rollout(
                new_rollout,
                thread_id="thread-new-source",
                provider="custom",
                cwd="/tmp/project-source",
            )
            _insert_thread(
                db_path,
                thread_id="thread-new-source",
                rollout_path=str(new_rollout),
                provider="custom",
                cwd="/tmp/project-source",
                archived=False,
                updated_at_ms=2000,
            )
            (codex_home / "config.toml").write_text(
                '[model_providers.openai]\nname = "openai"\n',
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            report = service.status()
            self.assertEqual(report.current_provider, "custom")
            self.assertEqual(report.current_provider_source, "recent_thread")

    def test_status_reports_provider_and_cwd_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-1.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-1",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-1",
                rollout_path=str(sessions_path),
                provider="sub2api",
                cwd="/tmp/project-b",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="sub2api", providers=["sub2api"])

            service = CodexProviderSyncService(codex_home)
            report = service.status()

            self.assertEqual(report.total_rollouts, 1)
            self.assertEqual(report.total_db_threads, 1)
            self.assertGreaterEqual(report.provider_mismatch_count, 1)
            self.assertGreaterEqual(report.cwd_mismatch_count, 1)
            self.assertEqual(report.preview[0]["thread_id"], "thread-1")

    def test_sync_updates_rollout_and_state_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-1.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-1",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-1",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="sub2api", providers=["sub2api"])

            service = CodexProviderSyncService(codex_home)
            report = service.sync(target_provider="sub2api", target_cwd="/tmp/project-b")

            self.assertTrue(report.changed)
            self.assertEqual(report.updated_rollouts, 1)
            self.assertEqual(report.updated_db_rows, 1)
            self.assertIsNotNone(report.backup_dir)

            first_line = sessions_path.read_text(encoding="utf-8").splitlines()[0]
            payload = json.loads(first_line)
            self.assertEqual(payload["payload"]["model_provider"], "sub2api")
            self.assertEqual(payload["payload"]["cwd"], "/tmp/project-b")

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT model_provider, cwd, rollout_path, updated_at, updated_at_ms FROM threads WHERE id = ?",
                ("thread-1",),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "sub2api")
            self.assertEqual(row[1], "/tmp/project-b")
            self.assertEqual(row[2], str(sessions_path.resolve()))
            self.assertEqual(row[3], 1)
            self.assertEqual(row[4], 1000)

    def test_switch_updates_config_and_syncs_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            archived_path = codex_home / "archived_sessions" / "rollout-thread-2.jsonl"
            _init_db(db_path)
            _write_rollout(
                archived_path,
                thread_id="thread-2",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-2",
                rollout_path=str(archived_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=True,
            )
            _write_config(codex_home / "config.toml", current="openai", providers=["openai", "sub2api"])

            service = CodexProviderSyncService(codex_home)
            report = service.switch_provider(
                provider="sub2api",
                model="gpt-5.5",
                sync_history=True,
            )

            self.assertTrue(report.config_updated)
            self.assertIsNotNone(report.sync)
            self.assertEqual(report.sync.updated_rollouts, 1)
            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('model_provider = "sub2api"', config_text)
            self.assertIn('model = "gpt-5.5"', config_text)
            self.assertIn("[model_providers.openai]", config_text)
            self.assertIn("[model_providers.sub2api]", config_text)

    def test_latest_history_provider_uses_newest_rollout_when_db_row_is_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            old_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-old.jsonl"
            latest_path = codex_home / "sessions" / "2026" / "05" / "09" / "rollout-thread-latest.jsonl"
            _init_db(db_path)
            _write_rollout(
                old_path,
                thread_id="thread-old",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-old",
                rollout_path=str(old_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
                updated_at_ms=1000,
            )
            _write_rollout(
                latest_path,
                thread_id="thread-latest",
                provider="custom",
                cwd="/tmp/project-a",
            )
            os.utime(old_path, (1, 1))
            os.utime(latest_path, (3, 3))

            service = CodexProviderSyncService(codex_home)

            self.assertEqual(service.latest_history_provider(), "custom")

    def test_restore_recovers_previous_config_and_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-3.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-3",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-3",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="openai", providers=["openai", "sub2api"])

            service = CodexProviderSyncService(codex_home)
            sync_report = service.switch_provider(provider="sub2api", sync_history=True)
            backup_dir = Path(sync_report.backup_dir or "")

            restore_report = service.restore(backup_dir)
            self.assertGreaterEqual(restore_report.restored_files, 2)
            self.assertEqual(
                (codex_home / "config.toml").read_text(encoding="utf-8"),
                'model_provider = "openai"\n\n[model_providers.openai]\nname = "test"\n\n[model_providers.sub2api]\nname = "test"\n',
            )
            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT model_provider FROM threads WHERE id = ?",
                ("thread-3",),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "openai")

    def test_sync_updates_global_state_workspace_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-4.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-4",
                provider="openai",
                cwd="/tmp/project-visible",
            )
            _insert_thread(
                db_path,
                thread_id="thread-4",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-visible",
                archived=False,
                updated_at_ms=5000,
            )
            _write_config(codex_home / "config.toml", current="openai", providers=["openai"])
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps({"electron-saved-workspace-roots": ["/tmp/old-project"]}),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            status = service.status()
            self.assertEqual(status.missing_workspace_roots, ["/tmp/project-visible"])
            self.assertTrue(status.project_visibility[0]["visible_in_first_50"])

            report = service.sync(target_provider="openai")
            self.assertEqual(report.updated_workspace_roots, 1)
            self.assertEqual(report.saved_workspace_root_count, 2)

            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertIn("/tmp/project-visible", global_state["electron-saved-workspace-roots"])
            self.assertIn("/tmp/project-visible", global_state["project-order"])

    def test_hide_stale_workspaces_removes_generated_codex_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            stale_root = codex_home / "Documents" / "Codex" / "2026-04-23-sub2api-ccswitch-api-sub2api-bad-request"
            stale_root.mkdir(parents=True)
            (stale_root / "scratch.txt").write_text("keep me", encoding="utf-8")
            existing_generated_root = codex_home / "Documents" / "Codex" / "2026-04-24" / "new-chat"
            existing_generated_root.mkdir(parents=True)
            normal_root = codex_home / "Projects" / "real-project"
            normal_root.mkdir(parents=True)
            archived_rollout = codex_home / "archived_sessions" / "rollout-thread-archived.jsonl"
            archived_rollout.parent.mkdir(parents=True)
            archived_rollout.write_text("{}\n", encoding="utf-8")
            removed_rollout = codex_home / "sessions" / "2026" / "04" / "26" / "rollout-thread-removed.jsonl"
            _write_rollout(
                removed_rollout,
                thread_id="thread-removed",
                provider="openai",
                cwd=str(stale_root),
            )
            live_orphan_rollout = codex_home / "sessions" / "2026" / "05" / "09" / "rollout-thread-live-orphan.jsonl"
            _write_rollout(
                live_orphan_rollout,
                thread_id="thread-live-orphan",
                provider="openai",
                cwd=str(normal_root),
            )
            _insert_thread(
                db_path,
                thread_id="thread-stale",
                rollout_path=str(codex_home / "sessions" / "rollout-thread-stale.jsonl"),
                provider="openai",
                cwd=str(stale_root),
                archived=False,
            )
            _insert_thread(
                db_path,
                thread_id="thread-existing-generated",
                rollout_path=str(codex_home / "sessions" / "rollout-thread-existing.jsonl"),
                provider="openai",
                cwd=str(existing_generated_root),
                archived=False,
            )
            _insert_thread(
                db_path,
                thread_id="thread-normal",
                rollout_path=str(codex_home / "sessions" / "rollout-thread-normal.jsonl"),
                provider="openai",
                cwd=str(normal_root),
                archived=False,
            )
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [
                            str(stale_root),
                            str(existing_generated_root),
                            str(normal_root),
                        ],
                        "active-workspace-roots": [str(stale_root)],
                        "project-order": [str(stale_root), str(normal_root)],
                        "sidebar-collapsed-groups": {
                            str(stale_root): True,
                            str(normal_root): False,
                        },
                        "electron-persisted-atom-state": {
                            "sidebar-collapsed-groups": {
                                str(stale_root): True,
                                str(normal_root): False,
                            }
                        },
                        "projectless-thread-ids": ["thread-stale", "thread-removed", "thread-missing"],
                        "thread-workspace-root-hints": {
                            "thread-stale": str(stale_root),
                            "thread-removed": str(stale_root),
                            "thread-missing": str(stale_root),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (codex_home / "session_index.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"id": "thread-stale", "thread_name": "bad request"}),
                        json.dumps({"id": "thread-removed", "thread_name": "removed"}),
                        json.dumps({"id": "thread-normal", "thread_name": "normal"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            preview = service.hide_stale_workspaces(dry_run=True)
            self.assertTrue(preview.changed)
            self.assertEqual(preview.matched_thread_ids, ["thread-stale", "thread-existing-generated"])
            self.assertEqual(preview.removed_session_index_entries, ["thread-stale", "thread-removed"])
            self.assertIn(str(stale_root), preview.removed_workspace_roots)
            self.assertEqual(
                [Path(path).name for path in preview.hidden_workspace_roots],
                [stale_root.name, existing_generated_root.name],
            )
            self.assertEqual([Path(path).name for path in preview.hidden_archived_rollouts], [archived_rollout.name])
            self.assertEqual([Path(path).name for path in preview.hidden_removed_rollouts], [removed_rollout.name])

            report = service.hide_stale_workspaces(dry_run=False)
            self.assertTrue(report.changed)
            self.assertEqual(report.removed_thread_rows, 2)
            self.assertEqual(report.removed_session_index_entries, ["thread-stale", "thread-removed"])
            self.assertEqual(
                [Path(path).name for path in report.hidden_workspace_roots],
                [stale_root.name, existing_generated_root.name],
            )
            self.assertEqual([Path(path).name for path in report.hidden_archived_rollouts], [archived_rollout.name])
            self.assertEqual([Path(path).name for path in report.hidden_removed_rollouts], [removed_rollout.name])
            self.assertIsNotNone(report.backup_dir)

            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT id FROM threads ORDER BY id").fetchall()
            conn.close()
            self.assertEqual([row[0] for row in rows], ["thread-normal"])
            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn(str(stale_root), global_state["electron-saved-workspace-roots"])
            self.assertNotIn(str(stale_root), global_state["active-workspace-roots"])
            self.assertNotIn(str(stale_root), global_state["project-order"])
            self.assertNotIn(str(stale_root), global_state["sidebar-collapsed-groups"])
            self.assertNotIn(
                str(stale_root),
                global_state["electron-persisted-atom-state"]["sidebar-collapsed-groups"],
            )
            self.assertNotIn(str(existing_generated_root), global_state["electron-saved-workspace-roots"])
            self.assertIn(str(normal_root), global_state["project-order"])
            self.assertIn(str(normal_root), global_state["sidebar-collapsed-groups"])
            self.assertIn(
                str(normal_root),
                global_state["electron-persisted-atom-state"]["sidebar-collapsed-groups"],
            )
            self.assertEqual(global_state["projectless-thread-ids"], [])
            self.assertEqual(global_state["thread-workspace-root-hints"], {})
            self.assertFalse(archived_rollout.exists())
            self.assertTrue((service.hidden_archived_root / archived_rollout.name).exists())
            self.assertFalse(removed_rollout.exists())
            self.assertTrue((service.hidden_removed_root / removed_rollout.name).exists())
            session_index = (codex_home / "session_index.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("thread-stale", session_index)
            self.assertNotIn("thread-removed", session_index)
            self.assertIn("thread-normal", session_index)
            self.assertFalse(stale_root.exists())
            self.assertEqual(
                (service.hidden_workspace_root / stale_root.name / "scratch.txt").read_text(encoding="utf-8"),
                "keep me",
            )
            self.assertTrue(live_orphan_rollout.exists())

    def test_hide_stale_workspaces_removes_hidden_rollout_db_and_index_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            service = CodexProviderSyncService(codex_home)
            hidden_id = "019dba88-d605-7a63-af50-8b1103cccfe2"
            active_id = "019e0814-0802-7c20-af23-c89309a24bab"
            hidden_rollout = service.hidden_archived_root / f"rollout-2026-04-23T21-30-23-{hidden_id}.jsonl"
            _write_rollout(hidden_rollout, thread_id=hidden_id, provider="openai", cwd=str(codex_home))
            _insert_thread(
                db_path,
                thread_id=hidden_id,
                rollout_path=str(hidden_rollout),
                provider="openai",
                cwd=str(codex_home),
                archived=False,
            )
            _insert_thread(
                db_path,
                thread_id=active_id,
                rollout_path=str(codex_home / "sessions" / "rollout-active.jsonl"),
                provider="openai",
                cwd=str(codex_home),
                archived=False,
            )
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "prompt-history": {
                            hidden_id: ["hidden prompt"],
                            active_id: ["active prompt"],
                        },
                        "conversation-permission-settings": {
                            hidden_id: {"approvalPolicy": "on-request"},
                            active_id: {"approvalPolicy": "on-request"},
                        },
                        "projectless-thread-ids": [hidden_id, active_id],
                        "thread-workspace-root-hints": {
                            hidden_id: str(codex_home),
                            active_id: str(codex_home),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (codex_home / "session_index.jsonl").write_text(
                "\n".join(
                    [
                        json.dumps({"id": hidden_id, "thread_name": "hidden"}),
                        json.dumps({"id": active_id, "thread_name": "active"}),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            report = service.hide_stale_workspaces(dry_run=False)

            self.assertEqual(report.matched_thread_ids, [hidden_id])
            self.assertEqual(report.removed_thread_rows, 1)
            self.assertEqual(report.removed_session_index_entries, [hidden_id])
            conn = sqlite3.connect(db_path)
            rows = conn.execute("SELECT id FROM threads ORDER BY id").fetchall()
            conn.close()
            self.assertEqual([row[0] for row in rows], [active_id])
            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn(hidden_id, global_state["prompt-history"])
            self.assertNotIn(hidden_id, global_state["conversation-permission-settings"])
            self.assertIn(active_id, global_state["prompt-history"])
            self.assertIn(active_id, global_state["conversation-permission-settings"])
            self.assertEqual(global_state["projectless-thread-ids"], [active_id])
            self.assertEqual(global_state["thread-workspace-root-hints"], {active_id: str(codex_home)})
            session_index = (codex_home / "session_index.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(hidden_id, session_index)
            self.assertIn(active_id, session_index)

    def test_hide_stale_workspaces_keeps_non_thread_id_keys_in_global_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            service = CodexProviderSyncService(codex_home)
            hidden_id = "019dba88-d605-7a63-af50-8b1103cccfe2"
            hidden_rollout = service.hidden_archived_root / f"rollout-2026-04-23T21-30-23-{hidden_id}.jsonl"
            _write_rollout(hidden_rollout, thread_id=hidden_id, provider="openai", cwd=str(codex_home))
            _insert_thread(
                db_path,
                thread_id=hidden_id,
                rollout_path=str(hidden_rollout),
                provider="openai",
                cwd=str(codex_home),
                archived=False,
            )
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "prompt-history": {hidden_id: ["hidden prompt"]},
                        "custom-mapping": {
                            hidden_id: {"should_stay": True},
                            "other-key": {"ok": True},
                        },
                    }
                ),
                encoding="utf-8",
            )

            service.hide_stale_workspaces(dry_run=False)
            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn(hidden_id, global_state["prompt-history"])
            self.assertIn(hidden_id, global_state["custom-mapping"])
            self.assertIn("other-key", global_state["custom-mapping"])

    def test_hide_stale_workspaces_parses_hidden_thread_ids_from_rollout_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            service = CodexProviderSyncService(codex_home)
            hidden_id = "thread-hidden-custom-format"
            hidden_rollout = service.hidden_archived_root / "rollout-2026-04-23T21-30-23-random-suffix.jsonl"
            _write_rollout(hidden_rollout, thread_id=hidden_id, provider="openai", cwd=str(codex_home))
            _insert_thread(
                db_path,
                thread_id=hidden_id,
                rollout_path=str(hidden_rollout),
                provider="openai",
                cwd=str(codex_home),
                archived=False,
            )

            report = service.hide_stale_workspaces(dry_run=False)
            self.assertEqual(report.matched_thread_ids, [hidden_id])
            conn = sqlite3.connect(db_path)
            row = conn.execute("SELECT id FROM threads WHERE id = ?", (hidden_id,)).fetchone()
            conn.close()
            self.assertIsNone(row)

    def test_hide_stale_workspaces_cleans_projectless_ids_when_no_known_threads_remain(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            _init_db(codex_home / "state_5.sqlite")
            bad_root = codex_home / "Documents" / "Codex" / "2026-04-23-sub2api-ccswitch-api-sub2api-bad-request"
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [str(bad_root)],
                        "projectless-thread-ids": ["thread-missing"],
                        "thread-workspace-root-hints": {"thread-missing": str(bad_root)},
                    }
                ),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            report = service.hide_stale_workspaces(dry_run=False)

            self.assertTrue(report.changed)
            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertEqual(global_state["electron-saved-workspace-roots"], [])
            self.assertEqual(global_state["projectless-thread-ids"], [])
            self.assertEqual(global_state["thread-workspace-root-hints"], {})

    def test_hide_stale_workspaces_discovers_unindexed_bad_request_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            stale_root = Path(tmp) / "Documents" / "Codex" / "2026-04-26" / "sub2api-ccswitch-sub2api-bad-request-400"
            stale_root.mkdir(parents=True)
            (stale_root / "scratch.txt").write_text("keep me too", encoding="utf-8")
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps({"electron-saved-workspace-roots": []}),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            preview = service.hide_stale_workspaces(dry_run=True)
            self.assertTrue(preview.changed)
            self.assertEqual([Path(path).name for path in preview.hidden_workspace_roots], [stale_root.name])

            report = service.hide_stale_workspaces(dry_run=False)
            self.assertTrue(report.changed)
            self.assertFalse(stale_root.exists())
            self.assertEqual(
                (service.hidden_workspace_root / stale_root.name / "scratch.txt").read_text(encoding="utf-8"),
                "keep me too",
            )

    def test_hide_stale_workspaces_removes_unbacked_generated_project_roots(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            visible_root = "/tmp/project-visible"
            stale_generated_root = str(Path(tmp) / "Documents" / "Codex" / "2026-04-28" / "new-chat")
            normal_extra_root = "/tmp/project-pinned"
            _insert_thread(
                db_path,
                thread_id="thread-visible",
                rollout_path=str(codex_home / "sessions" / "rollout-thread-visible.jsonl"),
                provider="openai",
                cwd=visible_root,
                archived=False,
            )
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [
                            stale_generated_root,
                            visible_root,
                            normal_extra_root,
                        ],
                        "project-order": [
                            stale_generated_root,
                            visible_root,
                            normal_extra_root,
                        ],
                        "sidebar-collapsed-groups": {
                            stale_generated_root: True,
                            visible_root: False,
                            normal_extra_root: True,
                        },
                    }
                ),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            report = service.hide_stale_workspaces(dry_run=False)

            self.assertTrue(report.changed)
            global_state = json.loads((codex_home / ".codex-global-state.json").read_text(encoding="utf-8"))
            self.assertNotIn(stale_generated_root, global_state["electron-saved-workspace-roots"])
            self.assertNotIn(stale_generated_root, global_state["project-order"])
            self.assertNotIn(stale_generated_root, global_state["sidebar-collapsed-groups"])
            self.assertIn(visible_root, global_state["electron-saved-workspace-roots"])
            self.assertIn(normal_extra_root, global_state["electron-saved-workspace-roots"])

    def test_hide_stale_workspaces_removes_live_generated_project_threads(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            db_path = codex_home / "state_5.sqlite"
            _init_db(db_path)
            nested_root = Path(tmp) / "Documents" / "Codex" / "2026-05-05" / "new-chat"
            top_level_root = Path(tmp) / "Documents" / "Codex" / "2026-05-03-claude-cli"
            nested_root.mkdir(parents=True)
            top_level_root.mkdir(parents=True)
            nested_rollout = codex_home / "sessions" / "2026" / "05" / "05" / "rollout-thread-nested.jsonl"
            top_level_rollout = codex_home / "sessions" / "2026" / "05" / "03" / "rollout-thread-top.jsonl"
            _write_rollout(nested_rollout, thread_id="thread-nested", provider="openai", cwd=str(nested_root))
            _write_rollout(top_level_rollout, thread_id="thread-top", provider="openai", cwd=str(top_level_root))
            _insert_thread(
                db_path,
                thread_id="thread-nested",
                rollout_path=str(nested_rollout),
                provider="openai",
                cwd=str(nested_root),
                archived=False,
                updated_at_ms=2000,
            )
            _insert_thread(
                db_path,
                thread_id="thread-top",
                rollout_path=str(top_level_rollout),
                provider="openai",
                cwd=str(top_level_root),
                archived=False,
                updated_at_ms=1000,
            )
            (codex_home / ".codex-global-state.json").write_text(
                json.dumps(
                    {
                        "electron-saved-workspace-roots": [str(nested_root), str(top_level_root)],
                        "project-order": [str(nested_root), str(top_level_root)],
                        "thread-workspace-root-hints": {
                            "thread-nested": str(nested_root),
                            "thread-top": str(top_level_root),
                        },
                    }
                ),
                encoding="utf-8",
            )
            (codex_home / "session_index.jsonl").write_text(
                json.dumps({"id": "thread-nested"}) + "\n" + json.dumps({"id": "thread-top"}) + "\n",
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            report = service.hide_stale_workspaces(dry_run=False)

            self.assertTrue(report.changed)
            self.assertEqual(report.removed_thread_rows, 2)
            self.assertEqual(sorted(report.removed_session_index_entries), ["thread-nested", "thread-top"])
            conn = sqlite3.connect(db_path)
            remaining = conn.execute("SELECT id FROM threads").fetchall()
            conn.close()
            self.assertEqual(remaining, [])
            self.assertFalse(nested_rollout.exists())
            self.assertFalse(top_level_rollout.exists())
            self.assertTrue((service.hidden_removed_root / nested_rollout.name).exists())
            self.assertTrue((service.hidden_removed_root / top_level_rollout.name).exists())
            self.assertFalse(nested_root.exists())
            self.assertFalse(top_level_root.exists())
            self.assertTrue((service.hidden_workspace_root / nested_root.name).exists())
            self.assertTrue((service.hidden_workspace_root / top_level_root.name).exists())

    def test_hide_stale_workspaces_migrates_legacy_hidden_items_outside_codex_home(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            _init_db(codex_home / "state_5.sqlite")
            service = CodexProviderSyncService(codex_home)
            legacy_archived = service.legacy_hidden_archived_root / "rollout-2026-04-23T21-30-23-019dba88-d605-7a63-af50-8b1103cccfe2.jsonl"
            legacy_removed = service.legacy_hidden_removed_root / "rollout-2026-04-26T10-58-22-019dc7b9-4b78-7b33-ac2f-472c846e3592.jsonl"
            legacy_workspace = service.legacy_hidden_workspace_root / "2026-04-25-new-chat"
            _write_rollout(legacy_archived, thread_id="019dba88-d605-7a63-af50-8b1103cccfe2", provider="openai", cwd=str(codex_home))
            _write_rollout(legacy_removed, thread_id="019dc7b9-4b78-7b33-ac2f-472c846e3592", provider="openai", cwd=str(codex_home))
            legacy_workspace.mkdir(parents=True)
            (legacy_workspace / "scratch.txt").write_text("migrate me", encoding="utf-8")

            report = service.hide_stale_workspaces(dry_run=False)

            self.assertTrue(report.changed)
            self.assertFalse(legacy_archived.exists())
            self.assertFalse(legacy_removed.exists())
            self.assertFalse(legacy_workspace.exists())
            self.assertTrue((service.hidden_archived_root / legacy_archived.name).exists())
            self.assertTrue((service.hidden_removed_root / legacy_removed.name).exists())
            self.assertEqual(
                (service.hidden_workspace_root / legacy_workspace.name / "scratch.txt").read_text(encoding="utf-8"),
                "migrate me",
            )

    def test_clear_electron_persisted_state_moves_local_storage_into_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            _init_db(codex_home / "state_5.sqlite")
            app_support_root = Path(tmp) / "Application Support" / "Codex"
            targets = [
                app_support_root / "Local Storage" / "leveldb" / "CURRENT",
                app_support_root / "Session Storage" / "CURRENT",
                app_support_root / "Partitions" / "codex-browser-app" / "Local Storage" / "leveldb" / "CURRENT",
                app_support_root / "Partitions" / "codex-browser-app" / "Session Storage" / "CURRENT",
            ]
            for path in targets:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("state", encoding="utf-8")

            service = CodexProviderSyncService(codex_home)
            report = service.clear_electron_persisted_state(dry_run=False, app_support_root=app_support_root)

            self.assertTrue(report.changed)
            self.assertIsNotNone(report.backup_dir)
            for path in targets:
                self.assertFalse(path.exists())
                moved = Path(report.backup_dir) / path.relative_to(app_support_root)
                self.assertTrue(moved.exists())
            manifest = json.loads((Path(report.backup_dir) / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(
                manifest["removed_paths"],
                [
                    "Local Storage",
                    "Session Storage",
                    "Partitions/codex-browser-app/Local Storage",
                    "Partitions/codex-browser-app/Session Storage",
                ],
            )

    def test_clear_electron_persisted_state_dry_run_leaves_files_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp) / ".codex"
            codex_home.mkdir()
            _init_db(codex_home / "state_5.sqlite")
            app_support_root = Path(tmp) / "Application Support" / "Codex"
            local_storage = app_support_root / "Local Storage" / "leveldb" / "CURRENT"
            local_storage.parent.mkdir(parents=True, exist_ok=True)
            local_storage.write_text("state", encoding="utf-8")

            service = CodexProviderSyncService(codex_home)
            report = service.clear_electron_persisted_state(dry_run=True, app_support_root=app_support_root)

            self.assertTrue(report.changed)
            self.assertIsNone(report.backup_dir)
            self.assertTrue(local_storage.exists())

    def test_sync_skips_locked_rollout_but_updates_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-5.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-5",
                provider="openai",
                cwd="/tmp/project-a",
            )
            sessions_path.with_suffix(sessions_path.suffix + ".lock").write_text("", encoding="utf-8")
            _insert_thread(
                db_path,
                thread_id="thread-5",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="sub2api", providers=["sub2api"])

            service = CodexProviderSyncService(codex_home)
            report = service.sync(target_provider="sub2api")

            self.assertEqual(report.updated_rollouts, 0)
            self.assertEqual(report.updated_db_rows, 1)
            self.assertEqual(report.skipped_locked_rollouts, [str(sessions_path.resolve())])
            first_line = sessions_path.read_text(encoding="utf-8").splitlines()[0]
            payload = json.loads(first_line)
            self.assertEqual(payload["payload"]["model_provider"], "openai")

            conn = sqlite3.connect(db_path)
            row = conn.execute(
                "SELECT model_provider FROM threads WHERE id = ?",
                ("thread-5",),
            ).fetchone()
            conn.close()
            self.assertEqual(row[0], "sub2api")

    def test_restore_can_skip_global_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-6.jsonl"
            _init_db(db_path)
            _write_rollout(
                sessions_path,
                thread_id="thread-6",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-6",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="openai", providers=["openai", "sub2api"])
            global_state_path = codex_home / ".codex-global-state.json"
            global_state_path.write_text(
                json.dumps({"electron-saved-workspace-roots": ["/tmp/project-a"]}),
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)
            sync_report = service.switch_provider(provider="sub2api", sync_history=True)
            backup_dir = Path(sync_report.backup_dir or "")
            global_state_path.write_text(
                json.dumps({"electron-saved-workspace-roots": ["/tmp/changed"]}),
                encoding="utf-8",
            )

            restore_report = service.restore(backup_dir, include_global_state=False)

            self.assertGreaterEqual(restore_report.restored_files, 2)
            self.assertEqual(restore_report.restored_global_state, 0)
            global_state = json.loads(global_state_path.read_text(encoding="utf-8"))
            self.assertEqual(global_state["electron-saved-workspace-roots"], ["/tmp/changed"])

    def test_switch_rejects_undefined_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            (codex_home / "config.toml").write_text(
                'model_provider = "openai"\n\n[model_providers.openai]\nname = "test"\n',
                encoding="utf-8",
            )

            service = CodexProviderSyncService(codex_home)

            with self.assertRaisesRegex(RuntimeError, "not defined"):
                service.switch_provider(provider="sub2api", sync_history=False)

    def test_status_reports_encrypted_content_risk(self):
        with tempfile.TemporaryDirectory() as tmp:
            codex_home = Path(tmp)
            db_path = codex_home / "state_5.sqlite"
            sessions_path = codex_home / "sessions" / "2026" / "05" / "08" / "rollout-thread-7.jsonl"
            _init_db(db_path)
            _write_rollout_with_encrypted_content(
                sessions_path,
                thread_id="thread-7",
                provider="openai",
                cwd="/tmp/project-a",
            )
            _insert_thread(
                db_path,
                thread_id="thread-7",
                rollout_path=str(sessions_path),
                provider="openai",
                cwd="/tmp/project-a",
                archived=False,
            )
            _write_config(codex_home / "config.toml", current="openai", providers=["openai"])

            service = CodexProviderSyncService(codex_home)
            status = service.status()
            sync = service.sync(target_provider="openai", dry_run=True)

            self.assertEqual(status.encrypted_content_threads, 1)
            self.assertEqual(status.encrypted_content_items, 1)
            self.assertEqual(status.encrypted_content_preview[0]["thread_id"], "thread-7")
            self.assertEqual(sync.encrypted_content_threads, 1)


if __name__ == "__main__":
    unittest.main()
