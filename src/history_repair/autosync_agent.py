from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import plistlib
import subprocess
import sys
from typing import Any


DEFAULT_LABEL = "com.codex.history-repair.provider-autosync"


@dataclass(frozen=True)
class AutosyncAgentReport:
    label: str
    plist_path: str
    log_dir: str
    installed: bool
    loaded: bool | None
    action: str
    command: list[str]
    message: str | None = None


class AutosyncLaunchAgent:
    def __init__(
        self,
        *,
        codex_home: str | Path | None = None,
        label: str = DEFAULT_LABEL,
        launch_agents_dir: str | Path | None = None,
    ):
        self.codex_home = Path(codex_home).expanduser().resolve() if codex_home else Path.home() / ".codex"
        self.label = label
        base_dir = Path(launch_agents_dir).expanduser() if launch_agents_dir else Path.home() / "Library" / "LaunchAgents"
        self.launch_agents_dir = base_dir.resolve()
        self.plist_path = self.launch_agents_dir / f"{self.label}.plist"
        self.log_dir = self.codex_home / "logs"

    def install(
        self,
        *,
        interval_sec: float = 5.0,
        switch_provider: bool = False,
        provider: str | None = None,
        load: bool = True,
    ) -> AutosyncAgentReport:
        command = _autosync_command(
            codex_home=self.codex_home,
            interval_sec=interval_sec,
            switch_provider=switch_provider,
            provider=provider,
        )
        self.launch_agents_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        payload = self._plist_payload(command)
        self.plist_path.write_bytes(plistlib.dumps(payload, sort_keys=False))
        loaded: bool | None = None
        message = None
        if load:
            loaded, message = self._reload()
        return AutosyncAgentReport(
            label=self.label,
            plist_path=str(self.plist_path),
            log_dir=str(self.log_dir),
            installed=True,
            loaded=loaded,
            action="install",
            command=command,
            message=message,
        )

    def uninstall(self, *, unload: bool = True) -> AutosyncAgentReport:
        command = []
        loaded: bool | None = None
        message = None
        if unload:
            loaded, message = self._unload(ignore_missing=True)
        if self.plist_path.exists():
            self.plist_path.unlink()
        return AutosyncAgentReport(
            label=self.label,
            plist_path=str(self.plist_path),
            log_dir=str(self.log_dir),
            installed=False,
            loaded=loaded,
            action="uninstall",
            command=command,
            message=message,
        )

    def status(self) -> AutosyncAgentReport:
        loaded, message = self._is_loaded()
        command = []
        if self.plist_path.exists():
            try:
                payload = plistlib.loads(self.plist_path.read_bytes())
                args = payload.get("ProgramArguments")
                if isinstance(args, list):
                    command = [str(item) for item in args]
            except (OSError, plistlib.InvalidFileException):
                command = []
        return AutosyncAgentReport(
            label=self.label,
            plist_path=str(self.plist_path),
            log_dir=str(self.log_dir),
            installed=self.plist_path.exists(),
            loaded=loaded,
            action="status",
            command=command,
            message=message,
        )

    def _plist_payload(self, command: list[str]) -> dict[str, Any]:
        stdout = self.log_dir / "provider-autosync.out.log"
        stderr = self.log_dir / "provider-autosync.err.log"
        return {
            "Label": self.label,
            "ProgramArguments": command,
            "RunAtLoad": True,
            "KeepAlive": True,
            "StandardOutPath": str(stdout),
            "StandardErrorPath": str(stderr),
            "WorkingDirectory": str(self.codex_home),
        }

    def _reload(self) -> tuple[bool, str | None]:
        self._unload(ignore_missing=True)
        domain = _launchctl_domain()
        bootstrap = subprocess.run(
            ["launchctl", "bootstrap", domain, str(self.plist_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if bootstrap.returncode != 0:
            return False, bootstrap.stderr.strip() or bootstrap.stdout.strip() or "launchctl bootstrap failed"
        subprocess.run(["launchctl", "enable", f"{domain}/{self.label}"], capture_output=True, check=False)
        subprocess.run(["launchctl", "kickstart", "-k", f"{domain}/{self.label}"], capture_output=True, check=False)
        return True, None

    def _unload(self, *, ignore_missing: bool) -> tuple[bool, str | None]:
        domain = _launchctl_domain()
        result = subprocess.run(
            ["launchctl", "bootout", domain, str(self.plist_path)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return False, None
        message = result.stderr.strip() or result.stdout.strip()
        if ignore_missing:
            return False, message or None
        return True, message or "launchctl bootout failed"

    def _is_loaded(self) -> tuple[bool, str | None]:
        result = subprocess.run(
            ["launchctl", "print", f"{_launchctl_domain()}/{self.label}"],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True, None
        return False, result.stderr.strip() or result.stdout.strip() or None


def _autosync_command(
    *,
    codex_home: Path,
    interval_sec: float,
    switch_provider: bool,
    provider: str | None,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "history_repair",
        "provider-autosync",
        "--codex-home",
        str(codex_home),
        "--interval-sec",
        str(max(float(interval_sec), 0.1)),
        "--quiet",
    ]
    if provider:
        command.extend(["--provider", provider])
    if switch_provider:
        command.append("--switch-provider")
    return command


def _launchctl_domain() -> str:
    return f"gui/{os.getuid()}"
