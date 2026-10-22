from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .storage import resolve_db_path

MANIFEST_SCHEMA = 1
SCHEDULE_SECONDS = 1800
MAC_LABEL = "com.openai.codex-usage.collect"
SYSTEMD_NAME = "codex-usage-collect"
WINDOWS_TASK_NAME_PREFIX = "Codex Usage Collector"
TASK_XML_NAMESPACE = "http://schemas.microsoft.com/windows/2004/02/mit/task"


class SchedulerError(RuntimeError):
    """An error while managing the user's scheduled collector."""


@dataclass(frozen=True)
class JobSpec:
    """Secret-free values that define one user's scheduled collector."""

    system: str
    root: str
    mise: str
    db_path: str
    environment_path: str
    log_path: str
    owner_id: str
    powershell: str = ""
    start_boundary: str = ""

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> JobSpec:
        expected = {field for field in cls.__dataclass_fields__}
        if set(value) != expected or any(
            not isinstance(item, str) for item in value.values()
        ):
            raise SchedulerError(
                "The schedule manifest has an invalid job specification."
            )
        spec = cls(**value)
        _validate_spec(spec)
        return spec


def _run(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            args,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
    except OSError as exc:
        command = Path(args[0]).name if args else "command"
        raise SchedulerError(f"Could not start {command}: {exc}") from exc
    if check and result.returncode != 0:
        command = Path(args[0]).name if args else "command"
        detail = result.stderr.strip() or result.stdout.strip()
        raise SchedulerError(
            f"{command} failed with exit code {result.returncode}"
            + (f": {detail}" if detail else ".")
        )
    return result


def _user_data_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        configured = os.environ.get("XDG_DATA_HOME")
        candidate = Path(configured).expanduser() if configured else None
        base = (
            candidate
            if candidate is not None and candidate.is_absolute()
            else home / ".local" / "share"
        )
    return base / "codex-usage"


def _manifest_path() -> Path:
    return _user_data_dir() / "schedule-manifest.json"


def _validate_value(name: str, value: str) -> None:
    if any(char in value for char in ("\r", "\n", "\0")):
        raise SchedulerError(f"{name} must not contain line breaks or NUL characters.")


def _validate_spec(spec: JobSpec) -> None:
    for name, value in asdict(spec).items():
        _validate_value(name, value)
    if spec.system not in {"Darwin", "Linux", "Windows"}:
        raise SchedulerError(f"Unsupported operating system: {spec.system}")
    for name in ("root", "mise", "db_path"):
        if not Path(getattr(spec, name)).is_absolute():
            raise SchedulerError(f"The scheduled {name} must be an absolute path.")
    if spec.system == "Darwin":
        if (
            not spec.owner_id.isdecimal()
            or spec.powershell
            or spec.start_boundary
            or not Path(spec.log_path).is_absolute()
        ):
            raise SchedulerError("The macOS schedule manifest is incomplete.")
    elif spec.system == "Linux":
        if any((spec.owner_id, spec.powershell, spec.start_boundary, spec.log_path)):
            raise SchedulerError("The Linux schedule manifest has unexpected fields.")
    elif (
        not Path(spec.powershell).is_absolute()
        or not spec.owner_id
        or not spec.start_boundary
        or not Path(spec.log_path).is_absolute()
    ):
        raise SchedulerError("The Windows schedule manifest is incomplete.")


def _current_spec() -> JobSpec:
    system = platform.system()
    if system not in {"Darwin", "Linux", "Windows"}:
        raise SchedulerError(f"Unsupported operating system: {system}")
    mise = shutil.which("mise")
    if mise is None:
        raise SchedulerError("Could not find mise on PATH.")
    db_path = resolve_db_path().expanduser().resolve()
    root = Path(__file__).resolve().parents[1]
    environment_path = os.environ.get("PATH", "")
    _validate_value("PATH", environment_path)
    log_path = ""
    owner_id = ""
    powershell = ""
    start_boundary = ""
    if system == "Darwin":
        owner_id = str(os.getuid())
        log_path = str(_user_data_dir() / "logs" / "scheduled-collect.log")
    elif system == "Windows":
        log_path = str(_user_data_dir() / "logs" / "scheduled-collect.log")
        powershell = shutil.which("powershell") or shutil.which("pwsh") or ""
        if not powershell:
            raise SchedulerError(
                "PowerShell is required to manage Windows Task Scheduler."
            )
        powershell = str(Path(powershell).resolve())
        owner_id = _windows_current_user_sid(powershell)
        start_boundary = (
            (datetime.now() + timedelta(minutes=1)).replace(microsecond=0).isoformat()
        )
    spec = JobSpec(
        system=system,
        root=str(root),
        mise=str(Path(mise).resolve()),
        db_path=str(db_path),
        environment_path=environment_path,
        log_path=log_path,
        owner_id=owner_id,
        powershell=powershell,
        start_boundary=start_boundary,
    )
    _validate_spec(spec)
    return spec


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _manifest_payload(spec: JobSpec) -> dict[str, Any]:
    return {"schemaVersion": MANIFEST_SCHEMA, "job": asdict(spec)}


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


def _load_manifest(path: Path) -> tuple[JobSpec | None, str | None]:
    if path.is_symlink():
        raise SchedulerError(
            "The schedule manifest is not a regular file; refusing to use it."
        )
    if not path.exists():
        return None, None
    if not path.is_file():
        raise SchedulerError(
            "The schedule manifest is not a regular file; refusing to use it."
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SchedulerError("The schedule manifest is unreadable or invalid.") from exc
    if not isinstance(document, dict) or set(document) != {"payload", "sha256"}:
        raise SchedulerError(
            "The schedule manifest has an unknown schema; refusing to use it."
        )
    payload = document["payload"]
    if (
        not isinstance(payload, dict)
        or set(payload) != {"schemaVersion", "job"}
        or payload["schemaVersion"] != MANIFEST_SCHEMA
        or not isinstance(document["sha256"], str)
        or document["sha256"] != _digest(payload)
    ):
        raise SchedulerError("The schedule manifest is modified or unsupported.")
    if not isinstance(payload["job"], dict):
        raise SchedulerError("The schedule manifest has an invalid job specification.")
    spec = JobSpec.from_dict(payload["job"])
    if spec.system != platform.system():
        raise SchedulerError(
            "The schedule manifest belongs to a different operating system."
        )
    return spec, document["sha256"]


def _write_manifest(path: Path, spec: JobSpec) -> None:
    payload = _manifest_payload(spec)
    document = {"payload": payload, "sha256": _digest(payload)}
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(
                json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
            )
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _atomic_write(path: Path, content: bytes) -> None:
    if path.is_symlink():
        raise SchedulerError(f"Refusing to replace a symbolic link: {path}")
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _ensure_log_directory(spec: JobSpec) -> None:
    if spec.log_path:
        Path(spec.log_path).parent.mkdir(parents=True, mode=0o700, exist_ok=True)


# LaunchAgent serialization and parsing are kept separate from launchctl operations.
def _mac_plist(spec: JobSpec) -> dict[str, Any]:
    return {
        "Label": MAC_LABEL,
        "ProgramArguments": [spec.mise, "run", "collect"],
        "WorkingDirectory": spec.root,
        "StartInterval": SCHEDULE_SECONDS,
        "EnvironmentVariables": {
            "PATH": spec.environment_path,
            "CODEX_USAGE_DB": spec.db_path,
        },
        "StandardOutPath": spec.log_path,
        "StandardErrorPath": spec.log_path,
    }


def _serialize_mac(spec: JobSpec) -> bytes:
    return plistlib.dumps(_mac_plist(spec), fmt=plistlib.FMT_XML, sort_keys=True)


def _parse_mac(content: bytes, spec: JobSpec) -> bool:
    try:
        parsed = plistlib.loads(content)
    except (plistlib.InvalidFileException, ValueError, TypeError):
        return False
    return parsed == _mac_plist(spec)


def _mac_agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{MAC_LABEL}.plist"


def _launchctl_target(spec: JobSpec) -> str:
    return f"gui/{spec.owner_id}/{MAC_LABEL}"


def _launchctl_loaded(spec: JobSpec) -> bool:
    result = _run(["launchctl", "print", _launchctl_target(spec)], check=False)
    if result.returncode == 0:
        return True
    if "Could not find service" in result.stderr or "No such process" in result.stderr:
        return False
    raise SchedulerError("Could not determine whether the LaunchAgent is loaded.")


def _mac_registration_exists(spec: JobSpec) -> bool:
    return _mac_agent_path().exists() or _launchctl_loaded(spec)


def _mac_registration_matches(spec: JobSpec) -> bool:
    path = _mac_agent_path()
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return _parse_mac(path.read_bytes(), spec)
    except OSError:
        return False


def _mac_apply(spec: JobSpec) -> None:
    _ensure_log_directory(spec)
    path = _mac_agent_path()
    if _launchctl_loaded(spec):
        _run(["launchctl", "bootout", _launchctl_target(spec)])
    _atomic_write(path, _serialize_mac(spec))
    _run(["launchctl", "bootstrap", f"gui/{spec.owner_id}", str(path)])


def _mac_remove(spec: JobSpec) -> None:
    target = _launchctl_target(spec)
    if _launchctl_loaded(spec):
        _run(["launchctl", "bootout", target])
    _mac_agent_path().unlink()


# systemd unit serialization/parsing are separate from systemctl operations.
def _systemd_escape(value: str) -> str:
    _validate_value("systemd unit value", value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    escaped = escaped.replace("%", "%%")
    return f'"{escaped}"'


def _systemd_escape_exec(value: str) -> str:
    return _systemd_escape(value).replace("$", "$$")


def _systemd_service(spec: JobSpec) -> bytes:
    lines = [
        "[Unit]",
        "Description=Codex usage collector",
        "",
        "[Service]",
        "Type=oneshot",
        f"WorkingDirectory={_systemd_escape(spec.root)}",
        f"Environment={_systemd_escape(f'PATH={spec.environment_path}')}",
        f"Environment={_systemd_escape(f'CODEX_USAGE_DB={spec.db_path}')}",
        f"ExecStart={_systemd_escape_exec(spec.mise)} run collect",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
    ]
    return "\n".join(lines).encode("utf-8")


def _systemd_timer() -> bytes:
    return (
        "[Unit]\n"
        "Description=Run Codex usage collector every 30 minutes\n"
        "\n"
        "[Timer]\n"
        "OnCalendar=*:0/30\n"
        "Persistent=true\n"
        f"Unit={SYSTEMD_NAME}.service\n"
        "AccuracySec=1min\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    ).encode()


def _systemd_unit_dir() -> Path:
    configured = os.environ.get("XDG_CONFIG_HOME")
    candidate = Path(configured).expanduser() if configured else None
    base = (
        candidate
        if candidate is not None and candidate.is_absolute()
        else Path.home() / ".config"
    )
    return base / "systemd" / "user"


def _systemd_paths() -> tuple[Path, Path]:
    directory = _systemd_unit_dir()
    return directory / f"{SYSTEMD_NAME}.service", directory / f"{SYSTEMD_NAME}.timer"


def _systemctl(
    args: list[str], *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    executable = shutil.which("systemctl")
    if executable is None:
        raise SchedulerError("systemctl is required for Linux scheduled collection.")
    return _run([executable, "--user", *args], check=check)


def _require_systemd_user_manager() -> None:
    result = _systemctl(["show-environment"], check=False)
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise SchedulerError(
            "The systemd user manager is unavailable; no cron fallback is used."
            + (f" ({detail})" if detail else "")
        )


def _systemd_load_state(unit: str) -> str:
    result = _systemctl(["show", "--property=LoadState", "--value", unit], check=False)
    if result.returncode != 0:
        raise SchedulerError("Could not inspect the systemd user unit.")
    return result.stdout.strip()


def _linux_registration_exists() -> bool:
    service_path, timer_path = _systemd_paths()
    artifacts_exist = any(
        path.exists() or path.is_symlink() for path in (service_path, timer_path)
    )
    if artifacts_exist:
        _require_systemd_user_manager()
        return True

    if shutil.which("systemctl") is None:
        return False
    try:
        manager = _systemctl(["show-environment"], check=False)
    except SchedulerError:
        return False
    if manager.returncode != 0:
        return False
    return any(
        _systemd_load_state(f"{SYSTEMD_NAME}.{suffix}") != "not-found"
        for suffix in ("service", "timer")
    )


def _linux_registration_matches(spec: JobSpec) -> bool:
    _require_systemd_user_manager()
    service_path, timer_path = _systemd_paths()
    if any(
        path.is_symlink() or not path.is_file() for path in (service_path, timer_path)
    ):
        return False
    try:
        return (
            service_path.read_bytes() == _systemd_service(spec)
            and timer_path.read_bytes() == _systemd_timer()
        )
    except OSError:
        return False


def _linux_apply(spec: JobSpec) -> None:
    _require_systemd_user_manager()
    service_path, timer_path = _systemd_paths()
    service_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    _atomic_write(service_path, _systemd_service(spec))
    _atomic_write(timer_path, _systemd_timer())
    _systemctl(["daemon-reload"])
    _systemctl(["enable", "--now", f"{SYSTEMD_NAME}.timer"])


def _linux_file_matches(path: Path, expected: bytes) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    try:
        return path.read_bytes() == expected
    except OSError:
        return False


def _linux_rollback_install(spec: JobSpec) -> None:
    """Remove only unit artifacts from a failed first installation."""
    service_path, timer_path = _systemd_paths()
    expected_files = {
        service_path: _systemd_service(spec),
        timer_path: _systemd_timer(),
    }
    matching_paths = {
        path
        for path, expected in expected_files.items()
        if _linux_file_matches(path, expected)
    }
    if not matching_paths:
        return

    errors: list[SchedulerError] = []
    timer_name = f"{SYSTEMD_NAME}.timer"
    timer_expected = expected_files[timer_path]
    owns_complete_pair = service_path in matching_paths and timer_path in matching_paths
    changed = False
    if owns_complete_pair and _linux_file_matches(timer_path, timer_expected):
        try:
            result = _systemctl(["disable", "--now", timer_name], check=False)
            if result.returncode != 0:
                try:
                    load_state = _systemd_load_state(timer_name)
                except SchedulerError as exc:
                    errors.append(exc)
                else:
                    if load_state != "not-found":
                        errors.append(
                            SchedulerError(
                                "Could not stop the partially installed systemd timer."
                            )
                        )
        except SchedulerError as exc:
            errors.append(exc)

        wants_directory = _systemd_unit_dir() / "timers.target.wants"
        link = wants_directory / timer_name
        if not wants_directory.is_symlink() and link.is_symlink():
            try:
                if link.resolve(strict=False) == timer_path.resolve(strict=False):
                    link.unlink()
                    changed = True
            except OSError as exc:
                errors.append(
                    SchedulerError(
                        "Could not remove the matching systemd timer enablement "
                        f"link: {exc}"
                    )
                )

    for path, expected in expected_files.items():
        if path == timer_path and not owns_complete_pair:
            continue
        if path in matching_paths and _linux_file_matches(path, expected):
            try:
                path.unlink()
                changed = True
            except OSError as exc:
                errors.append(
                    SchedulerError(
                        f"Could not remove the matching systemd unit file: "
                        f"{path.name}: {exc}"
                    )
                )

    if changed:
        try:
            _systemctl(["daemon-reload"])
        except SchedulerError as exc:
            errors.append(exc)
    if errors:
        raise SchedulerError(
            "The partial systemd installation could not be fully rolled back."
        ) from errors[0]


def _linux_remove(spec: JobSpec) -> None:
    _require_systemd_user_manager()
    service_path, timer_path = _systemd_paths()
    _systemctl(["disable", "--now", f"{SYSTEMD_NAME}.timer"], check=False)
    timer_path.unlink(missing_ok=True)
    service_path.unlink(missing_ok=True)
    _systemctl(["daemon-reload"])


def _powershell_script(
    powershell: str, script: str, *, check: bool = True
) -> subprocess.CompletedProcess[str]:
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return _run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ],
        check=check,
    )


def _windows_current_user_sid(powershell: str) -> str:
    result = _powershell_script(
        powershell,
        "[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value",
    )
    sid = result.stdout.strip()
    if not sid.startswith("S-"):
        raise SchedulerError("Could not determine the current Windows user SID.")
    return sid


def _windows_task_runner(spec: JobSpec) -> str:
    values = {
        "path": spec.environment_path,
        "database": spec.db_path,
        "root": spec.root,
        "mise": spec.mise,
        "log": spec.log_path,
    }
    encoded = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in values.items()
    }
    return "\n".join(
        [
            (
                "$decode = { param($value) "
                "[Text.Encoding]::UTF8.GetString("
                "[Convert]::FromBase64String($value)) }"
            ),
            f"$env:PATH = & $decode '{encoded['path']}'",
            f"$env:CODEX_USAGE_DB = & $decode '{encoded['database']}'",
            f"$root = & $decode '{encoded['root']}'",
            f"$mise = & $decode '{encoded['mise']}'",
            f"$log = & $decode '{encoded['log']}'",
            "Set-Location -LiteralPath $root",
            "try { & $mise run collect *>> $log; exit $LASTEXITCODE }",
            "catch { $_ | Out-File -LiteralPath $log -Append; exit 1 }",
        ]
    )


def _windows_task_xml_arguments(spec: JobSpec) -> str:
    runner = base64.b64encode(_windows_task_runner(spec).encode("utf-16le")).decode(
        "ascii"
    )
    return (
        f"-NoProfile -NonInteractive -ExecutionPolicy Bypass -EncodedCommand {runner}"
    )


def _windows_task_name(spec: JobSpec) -> str:
    """Keep the Task Scheduler entry separate for each Windows user."""
    return f"{WINDOWS_TASK_NAME_PREFIX} - {spec.owner_id}"


def _task_tag(name: str) -> str:
    return f"{{{TASK_XML_NAMESPACE}}}{name}"


def _windows_task_xml(spec: JobSpec) -> bytes:
    ET.register_namespace("", TASK_XML_NAMESPACE)
    tag = _task_tag
    root = ET.Element(tag("Task"), {"version": "1.3"})
    registration = ET.SubElement(root, tag("RegistrationInfo"))
    marker = f"codex-usage-managed:{_digest(_manifest_payload(spec))}"
    ET.SubElement(registration, tag("URI")).text = f"\\{_windows_task_name(spec)}"
    ET.SubElement(registration, tag("Description")).text = marker
    triggers = ET.SubElement(root, tag("Triggers"))
    daily = ET.SubElement(triggers, tag("CalendarTrigger"))
    ET.SubElement(daily, tag("Enabled")).text = "true"
    ET.SubElement(daily, tag("StartBoundary")).text = spec.start_boundary
    repetition = ET.SubElement(daily, tag("Repetition"))
    ET.SubElement(repetition, tag("Interval")).text = "PT30M"
    ET.SubElement(repetition, tag("Duration")).text = "P1D"
    ET.SubElement(repetition, tag("StopAtDurationEnd")).text = "false"
    ET.SubElement(
        ET.SubElement(daily, tag("ScheduleByDay")), tag("DaysInterval")
    ).text = "1"
    principals = ET.SubElement(root, tag("Principals"))
    principal = ET.SubElement(principals, tag("Principal"), {"id": "Author"})
    ET.SubElement(principal, tag("UserId")).text = spec.owner_id
    ET.SubElement(principal, tag("LogonType")).text = "InteractiveToken"
    ET.SubElement(principal, tag("RunLevel")).text = "LeastPrivilege"
    settings = ET.SubElement(root, tag("Settings"))
    for name, value in (
        ("MultipleInstancesPolicy", "IgnoreNew"),
        ("DisallowStartIfOnBatteries", "false"),
        ("StopIfGoingOnBatteries", "false"),
        ("StartWhenAvailable", "true"),
        ("AllowHardTerminate", "true"),
        ("Enabled", "true"),
        ("Hidden", "false"),
        ("RunOnlyIfNetworkAvailable", "false"),
        ("ExecutionTimeLimit", "PT30M"),
        ("Priority", "7"),
    ):
        ET.SubElement(settings, tag(name)).text = value
    actions = ET.SubElement(root, tag("Actions"), {"Context": "Author"})
    action = ET.SubElement(actions, tag("Exec"))
    ET.SubElement(action, tag("Command")).text = spec.powershell
    ET.SubElement(action, tag("Arguments")).text = _windows_task_xml_arguments(spec)
    ET.SubElement(action, tag("WorkingDirectory")).text = spec.root
    return ET.tostring(root, encoding="utf-8", xml_declaration=False)


def _parse_windows_registration(content: str, spec: JobSpec) -> bool:
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return False
    tag = _task_tag
    if root.tag != tag("Task") or root.attrib != {"version": "1.3"}:
        return False

    def has_children(element: ET.Element, names: tuple[str, ...]) -> bool:
        return [child.tag for child in element] == [tag(name) for name in names]

    def has_no_attributes(element: ET.Element) -> bool:
        return not element.attrib

    def leaf_matches(parent: ET.Element, name: str, value: str) -> bool:
        element = parent.find(tag(name))
        return (
            element is not None
            and element.tag == tag(name)
            and has_no_attributes(element)
            and not list(element)
            and element.text == value
        )

    if not has_children(
        root, ("RegistrationInfo", "Principals", "Settings", "Triggers", "Actions")
    ):
        return False
    registration = root.find(tag("RegistrationInfo"))
    marker = f"codex-usage-managed:{_digest(_manifest_payload(spec))}"
    if (
        registration is None
        or not has_no_attributes(registration)
        or not has_children(registration, ("Description", "URI"))
        or not leaf_matches(registration, "URI", f"\\{_windows_task_name(spec)}")
        or not leaf_matches(registration, "Description", marker)
    ):
        return False
    triggers = root.find(tag("Triggers"))
    daily = None if triggers is None else triggers.find(tag("CalendarTrigger"))
    if (
        triggers is None
        or not has_no_attributes(triggers)
        or not has_children(triggers, ("CalendarTrigger",))
        or daily is None
        or not has_no_attributes(daily)
    ):
        return False
    daily_names = [child.tag for child in daily]
    if daily_names != [
        tag("StartBoundary"),
        tag("Repetition"),
        tag("ScheduleByDay"),
    ] or not leaf_matches(daily, "StartBoundary", spec.start_boundary):
        return False
    repetition = daily.find(tag("Repetition"))
    schedule_by_day = daily.find(tag("ScheduleByDay"))
    if (
        repetition is None
        or not has_no_attributes(repetition)
        or not has_children(repetition, ("Interval", "Duration"))
        or not leaf_matches(repetition, "Interval", "PT30M")
        or not leaf_matches(repetition, "Duration", "P1D")
        or schedule_by_day is None
        or not has_no_attributes(schedule_by_day)
        or not has_children(schedule_by_day, ("DaysInterval",))
        or not leaf_matches(schedule_by_day, "DaysInterval", "1")
    ):
        return False
    principals = root.find(tag("Principals"))
    principal = None if principals is None else principals.find(tag("Principal"))
    if (
        principals is None
        or not has_no_attributes(principals)
        or not has_children(principals, ("Principal",))
        or principal is None
        or principal.attrib != {"id": "Author"}
        or not has_children(principal, ("UserId", "LogonType"))
        or not leaf_matches(principal, "UserId", spec.owner_id)
        or not leaf_matches(principal, "LogonType", "InteractiveToken")
    ):
        return False
    settings = root.find(tag("Settings"))
    # These are the exact settings emitted by Task Scheduler's canonical export
    # for our registered task. IdleSettings and UseUnifiedSchedulingEngine are
    # added by the exporter and are accepted only with these exact values.
    normalized_settings = {
        "DisallowStartIfOnBatteries": "false",
        "StopIfGoingOnBatteries": "false",
        "ExecutionTimeLimit": "PT30M",
        "MultipleInstancesPolicy": "IgnoreNew",
        "StartWhenAvailable": "true",
        "IdleSettings": None,
        "UseUnifiedSchedulingEngine": "true",
    }
    if settings is None or not has_no_attributes(settings):
        return False
    setting_elements = list(settings)
    setting_names = [element.tag for element in setting_elements]
    if setting_names != [tag(name) for name in normalized_settings]:
        return False
    for element in setting_elements:
        name = element.tag.removeprefix(f"{{{TASK_XML_NAMESPACE}}}")
        expected_value = normalized_settings[name]
        if name == "IdleSettings":
            if (
                element.attrib
                or not has_children(element, ("StopOnIdleEnd", "RestartOnIdle"))
                or not leaf_matches(element, "StopOnIdleEnd", "true")
                or not leaf_matches(element, "RestartOnIdle", "false")
            ):
                return False
            continue
        if (
            element.tag != tag(name)
            or not has_no_attributes(element)
            or list(element)
            or element.text != expected_value
        ):
            return False
    actions = root.find(tag("Actions"))
    action = None if actions is None else actions.find(tag("Exec"))
    return (
        actions is not None
        and actions.attrib == {"Context": "Author"}
        and has_children(actions, ("Exec",))
        and action is not None
        and has_no_attributes(action)
        and has_children(action, ("Command", "Arguments", "WorkingDirectory"))
        and leaf_matches(action, "Command", spec.powershell)
        and leaf_matches(action, "Arguments", _windows_task_xml_arguments(spec))
        and leaf_matches(action, "WorkingDirectory", spec.root)
    )


def _windows_export_task(spec: JobSpec) -> str | None:
    name = _windows_task_name(spec).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$task = Get-ScheduledTask -TaskName '{name}' "
        "-TaskPath '\\' -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { [Console]::Out.Write('NOT_FOUND'); exit 0 }; "
        f"Export-ScheduledTask -TaskName '{name}' -TaskPath '\\' | Out-String"
    )
    result = _powershell_script(spec.powershell, script)
    content = result.stdout.strip()
    return None if content == "NOT_FOUND" else content


def _windows_registration_exists(spec: JobSpec) -> bool:
    return _windows_export_task(spec) is not None


def _windows_registration_matches(spec: JobSpec) -> bool:
    content = _windows_export_task(spec)
    return content is not None and _parse_windows_registration(content, spec)


def _windows_apply(spec: JobSpec, *, replace_existing: bool = False) -> None:
    _ensure_log_directory(spec)
    name = _windows_task_name(spec).replace("'", "''")
    with tempfile.NamedTemporaryFile(
        prefix="codex-usage-task-", suffix=".xml", delete=False
    ) as temporary:
        task_xml_path = Path(temporary.name)
        temporary.write(_windows_task_xml(spec))

    try:
        encoded_path = base64.b64encode(str(task_xml_path).encode("utf-8")).decode(
            "ascii"
        )
        replace_flag = " -Force" if replace_existing else ""
        script = (
            "$ErrorActionPreference = 'Stop'; "
            f"$xmlPath = [Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{encoded_path}')); "
            "$xml = [IO.File]::ReadAllText($xmlPath, [Text.Encoding]::UTF8); "
            f"Register-ScheduledTask -TaskName '{name}' "
            "-TaskPath '\\' -Xml $xml"
            f"{replace_flag} | Out-Null"
        )
        _powershell_script(spec.powershell, script)
    finally:
        task_xml_path.unlink(missing_ok=True)


def _windows_remove(spec: JobSpec) -> None:
    name = _windows_task_name(spec).replace("'", "''")
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"Unregister-ScheduledTask -TaskName '{name}' -TaskPath '\\' -Confirm:$false"
    )
    _powershell_script(spec.powershell, script)


def _registration_exists(spec: JobSpec) -> bool:
    if spec.system == "Darwin":
        return _mac_registration_exists(spec)
    if spec.system == "Linux":
        return _linux_registration_exists()
    return _windows_registration_exists(spec)


def _registration_matches(spec: JobSpec) -> bool:
    if spec.system == "Darwin":
        return _mac_registration_matches(spec)
    if spec.system == "Linux":
        return _linux_registration_matches(spec)
    return _windows_registration_matches(spec)


def _apply_registration(spec: JobSpec, *, replace_existing: bool = False) -> None:
    if spec.system == "Darwin":
        _mac_apply(spec)
    elif spec.system == "Linux":
        _linux_apply(spec)
    else:
        _windows_apply(spec, replace_existing=replace_existing)


def _remove_registration(spec: JobSpec) -> None:
    if spec.system == "Darwin":
        _mac_remove(spec)
    elif spec.system == "Linux":
        _linux_remove(spec)
    else:
        _windows_remove(spec)


def _validate_owner(spec: JobSpec) -> None:
    if spec.system == "Darwin" and spec.owner_id != str(os.getuid()):
        raise SchedulerError("The schedule manifest belongs to a different macOS user.")
    if spec.system == "Windows":
        powershell = spec.powershell
        if not Path(powershell).exists():
            powershell = shutil.which("powershell") or shutil.which("pwsh") or ""
        if not powershell or _windows_current_user_sid(powershell) != spec.owner_id:
            raise SchedulerError(
                "The schedule manifest belongs to a different Windows user."
            )


def _managed_spec(path: Path) -> JobSpec | None:
    spec, _ = _load_manifest(path)
    if spec is None:
        return None
    _validate_owner(spec)
    if not _registration_matches(spec):
        raise SchedulerError(
            "The schedule manifest and operating-system registration do not "
            "match; refusing to modify it."
        )
    return spec


def _rollback_registration(previous: JobSpec | None, current: JobSpec) -> None:
    try:
        if previous is None:
            if current.system == "Linux":
                _linux_rollback_install(current)
                if _linux_registration_exists():
                    raise SchedulerError(
                        "Unmatched systemd schedule artifacts remain; they were "
                        "left unchanged."
                    )
            elif _registration_matches(current):
                _remove_registration(current)
            elif _registration_exists(current):
                raise SchedulerError(
                    "An unexpected schedule appeared during installation; "
                    "it was left unchanged."
                )
        else:
            _apply_registration(previous, replace_existing=True)
    except (OSError, SchedulerError) as exc:
        raise SchedulerError(
            "Schedule registration failed and its previous state could not be restored."
        ) from exc


def schedule(action: str) -> dict[str, Any]:
    """Install, inspect, or remove the current user's collector schedule."""
    if action not in {"install", "status", "uninstall"}:
        raise SchedulerError(f"Unsupported schedule action: {action}")
    manifest_path = _manifest_path()
    previous = _managed_spec(manifest_path)
    if previous is None:
        if _current_platform_registration_exists():
            raise SchedulerError(
                "A schedule exists without a valid manifest; refusing to modify it."
            )
        if action != "install":
            return {"installed": False, "intervalSeconds": SCHEDULE_SECONDS}
    if action == "status":
        assert previous is not None
        return {
            "installed": True,
            "intervalSeconds": SCHEDULE_SECONDS,
            "operatingSystem": previous.system,
            "repository": previous.root,
            "database": previous.db_path,
        }
    if action == "uninstall":
        assert previous is not None
        _remove_registration(previous)
        manifest_path.unlink()
        return {"installed": False, "intervalSeconds": SCHEDULE_SECONDS}
    current = _current_spec()
    try:
        _apply_registration(current, replace_existing=previous is not None)
    except (OSError, SchedulerError):
        _rollback_registration(previous, current)
        raise
    try:
        _write_manifest(manifest_path, current)
    except OSError as exc:
        _rollback_registration(previous, current)
        raise SchedulerError(
            "Could not save the schedule manifest after registration."
        ) from exc
    return {
        "installed": True,
        "intervalSeconds": SCHEDULE_SECONDS,
        "operatingSystem": current.system,
        "repository": current.root,
        "database": current.db_path,
    }


def _current_platform_registration_exists() -> bool:
    system = platform.system()
    if system not in {"Darwin", "Linux", "Windows"}:
        raise SchedulerError(f"Unsupported operating system: {system}")
    if system == "Darwin":
        spec = JobSpec(
            system=system,
            root=str(Path.cwd().resolve()),
            mise="/usr/bin/true",
            db_path=str(resolve_db_path().resolve()),
            environment_path=os.environ.get("PATH", ""),
            log_path=str(_user_data_dir() / "logs" / "scheduled-collect.log"),
            owner_id=str(os.getuid()),
        )
        return _mac_registration_exists(spec)
    if system == "Linux":
        return _linux_registration_exists()
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if powershell is None:
        raise SchedulerError(
            "PowerShell is required to inspect Windows Task Scheduler."
        )
    spec = JobSpec(
        system=system,
        root=str(Path.cwd().resolve()),
        mise="C:/mise.exe",
        db_path=str(resolve_db_path().resolve()),
        environment_path=os.environ.get("PATH", ""),
        log_path=str(_user_data_dir() / "logs" / "scheduled-collect.log"),
        owner_id=_windows_current_user_sid(str(Path(powershell).resolve())),
        powershell=str(Path(powershell).resolve()),
    )
    return _windows_registration_exists(spec)
