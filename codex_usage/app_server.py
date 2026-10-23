from __future__ import annotations

import contextlib
import json
import math
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from typing import Any, TextIO

REQUEST_TIMEOUT_SECONDS = 15.0
GRACEFUL_EXIT_TIMEOUT_SECONDS = 0.5
FORCED_EXIT_TIMEOUT_SECONDS = 3.0
READER_JOIN_TIMEOUT_SECONDS = 0.75
CODEX_SHIM_ENV = "CODEX_USAGE_CODEX_SHIM"
STDERR_TAIL_LIMIT = 64 * 1024
WINDOWS_LAUNCHER_SIGNAL = b"\0"
# Read exactly one byte so the launcher cannot buffer the later JSON-RPC input.
WINDOWS_LAUNCHER_CODE = (
    "import json, os, subprocess, sys\n"
    "if os.read(0, 1) != b'\\0':\n"
    "    raise SystemExit(125)\n"
    "command = json.loads(sys.argv[1])\n"
    "try:\n"
    "    child = subprocess.Popen(\n"
    "        command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr\n"
    "    )\n"
    "except OSError as exc:\n"
    "    print(f'Could not start Codex App Server: {exc}', file=sys.stderr)\n"
    "    raise SystemExit(127)\n"
    "raise SystemExit(child.wait())\n"
)


class AppServerError(RuntimeError):
    """An error while communicating with or interpreting Codex App Server."""


class _StderrTail:
    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._parts: deque[str] = deque()
        self._length = 0
        self._lock = threading.Lock()

    def append(self, text: str) -> None:
        with self._lock:
            self._parts.append(text)
            self._length += len(text)
            while self._length > self._limit and self._parts:
                excess = self._length - self._limit
                first = self._parts[0]
                if len(first) <= excess:
                    self._parts.popleft()
                    self._length -= len(first)
                else:
                    self._parts[0] = first[excess:]
                    self._length -= excess

    def get(self) -> str:
        with self._lock:
            return "".join(self._parts).strip()


def _read_stdout(stream: TextIO, events: queue.Queue[tuple[str, Any]]) -> None:
    try:
        for line in stream:
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                events.put(
                    (
                        "error",
                        "Codex App Server returned a line that is not valid JSON.",
                    )
                )
                return
            if not isinstance(message, dict):
                events.put(
                    (
                        "error",
                        "Codex App Server returned a JSON value that is not an object.",
                    )
                )
                return
            events.put(("message", message))
    except (OSError, UnicodeError) as exc:
        events.put(("error", f"Could not read Codex App Server output: {exc}"))
    finally:
        events.put(("eof", None))


def _drain_stderr(stream: TextIO, tail: _StderrTail) -> None:
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                return
            tail.append(chunk)
    except OSError, UnicodeError:
        return


def _send_message(stream: TextIO, message: dict[str, Any]) -> None:
    try:
        stream.write(
            json.dumps(message, separators=(",", ":"), ensure_ascii=False) + "\n"
        )
        stream.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise AppServerError(
            f"Could not send a request to Codex App Server: {exc}"
        ) from exc


def _start_windows_launcher(stream: TextIO) -> None:
    try:
        stream.flush()
        stream.buffer.write(WINDOWS_LAUNCHER_SIGNAL)
        stream.buffer.flush()
    except (BrokenPipeError, OSError, ValueError) as exc:
        raise AppServerError(
            f"Could not release the Windows Codex launcher: {exc}"
        ) from exc


def _receive_response(
    events: queue.Queue[tuple[str, Any]],
    process: subprocess.Popen[str],
    request_id: int,
) -> dict[str, Any]:
    deadline = time.monotonic() + REQUEST_TIMEOUT_SECONDS
    timeout_message = (
        f"Timed out after {REQUEST_TIMEOUT_SECONDS:g} seconds waiting for "
        f"App Server request {request_id}."
    )
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AppServerError(timeout_message)
        try:
            event, value = events.get(timeout=remaining)
        except queue.Empty as exc:
            raise AppServerError(timeout_message) from exc

        if event == "error":
            raise AppServerError(value)
        if event == "eof":
            raise AppServerError(
                "Codex App Server closed stdout before responding to request "
                f"{request_id} (exit code {process.poll()})."
            )

        # Notifications have no id. Ignore them and any unrelated responses.
        response_id = value.get("id")
        if type(response_id) is not int or response_id != request_id:
            continue
        if "error" in value:
            error = value["error"]
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message", "unknown server error")
                code_text = f" (code {code})" if code is not None else ""
                raise AppServerError(
                    f"App Server request {request_id} failed{code_text}: {message}"
                )
            raise AppServerError(f"App Server request {request_id} failed: {error}")
        if "result" not in value:
            raise AppServerError(
                f"App Server response {request_id} has neither a result nor an error."
            )
        return value


class _WindowsJob:
    def __init__(self, kernel32: Any, handle: Any) -> None:
        self._kernel32 = kernel32
        self._handle = handle

    @classmethod
    def create(cls) -> _WindowsJob | None:
        if os.name != "nt":
            return None
        try:
            import ctypes
            from ctypes import wintypes

            class BasicLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD),
                ]

            class IoCounters(ctypes.Structure):
                _fields_ = [
                    ("ReadOperationCount", ctypes.c_ulonglong),
                    ("WriteOperationCount", ctypes.c_ulonglong),
                    ("OtherOperationCount", ctypes.c_ulonglong),
                    ("ReadTransferCount", ctypes.c_ulonglong),
                    ("WriteTransferCount", ctypes.c_ulonglong),
                    ("OtherTransferCount", ctypes.c_ulonglong),
                ]

            class ExtendedLimitInformation(ctypes.Structure):
                _fields_ = [
                    ("BasicLimitInformation", BasicLimitInformation),
                    ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
            kernel32.CreateJobObjectW.restype = wintypes.HANDLE
            kernel32.SetInformationJobObject.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.LPVOID,
                wintypes.DWORD,
            ]
            kernel32.SetInformationJobObject.restype = wintypes.BOOL
            kernel32.AssignProcessToJobObject.argtypes = [
                wintypes.HANDLE,
                wintypes.HANDLE,
            ]
            kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
            kernel32.OpenProcess.argtypes = [
                wintypes.DWORD,
                wintypes.BOOL,
                wintypes.DWORD,
            ]
            kernel32.OpenProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel32.TerminateJobObject.restype = wintypes.BOOL

            handle = kernel32.CreateJobObjectW(None, None)
            if not handle:
                return None
            information = ExtendedLimitInformation()
            information.BasicLimitInformation.LimitFlags = (
                0x00002000  # KILL_ON_JOB_CLOSE
            )
            if not kernel32.SetInformationJobObject(
                handle,
                9,  # JobObjectExtendedLimitInformation
                ctypes.byref(information),
                ctypes.sizeof(information),
            ):
                kernel32.CloseHandle(handle)
                return None
            return cls(kernel32, handle)
        except AttributeError, OSError, TypeError, ValueError:
            return None

    def assign(self, process_id: int) -> bool:
        if not self._handle:
            return False
        process_set_quota = 0x0100
        process_terminate = 0x0001
        process_handle = self._kernel32.OpenProcess(
            process_set_quota | process_terminate,
            False,
            process_id,
        )
        if not process_handle:
            return False
        try:
            return bool(
                self._kernel32.AssignProcessToJobObject(self._handle, process_handle)
            )
        finally:
            self._kernel32.CloseHandle(process_handle)

    def close(self) -> bool:
        if not self._handle:
            return True
        if not self._kernel32.CloseHandle(self._handle):
            self._kernel32.TerminateJobObject(self._handle, 1)
            if not self._kernel32.CloseHandle(self._handle):
                return False
        self._handle = None
        return True


def _windows_shim_command(shim: str) -> tuple[str, dict[str, str]]:
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    command_processor = os.path.join(system_root, "System32", "cmd.exe")
    if not os.path.isfile(command_processor):
        raise AppServerError(
            "Could not locate the Windows command processor (cmd.exe)."
        )

    child_env = os.environ.copy()
    child_env[CODEX_SHIM_ENV] = os.path.abspath(shim)
    # Keep the shim path out of cmd's command text. Expansion happens inside
    # quotes, with delayed expansion and AutoRun disabled, so spaces and cmd
    # metacharacters in a PATH entry remain part of the executable path.
    command_line = (
        f'"{command_processor}" /d /v:off /s /c '
        f'""%{CODEX_SHIM_ENV}%" app-server --stdio"'
    )
    return command_line, child_env


def _resolve_codex_command() -> tuple[str | list[str], dict[str, str] | None]:
    if os.name != "nt":
        return ["codex", "app-server", "--stdio"], None

    executable = shutil.which("codex.exe")
    if executable is not None:
        return [executable, "app-server", "--stdio"], None

    shim = shutil.which("codex.cmd") or shutil.which("codex.bat")
    if shim is not None:
        return _windows_shim_command(shim)

    executable = shutil.which("codex")
    if executable is not None:
        if os.path.splitext(executable)[1].casefold() in {".cmd", ".bat"}:
            return _windows_shim_command(executable)
        return [executable, "app-server", "--stdio"], None
    return ["codex", "app-server", "--stdio"], None


def _windows_launcher_command(command: str | list[str]) -> list[str]:
    return [
        sys.executable,
        "-I",
        "-S",
        "-c",
        WINDOWS_LAUNCHER_CODE,
        json.dumps(command, separators=(",", ":"), ensure_ascii=False),
    ]


def _taskkill_path() -> str | None:
    if os.name != "nt":
        return None
    system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    candidate = os.path.join(system_root, "System32", "taskkill.exe")
    return candidate if os.path.isfile(candidate) else None


def _force_terminate_tree(
    process: subprocess.Popen[str],
    process_tree: _WindowsJob | None = None,
) -> None:
    if os.name == "nt":
        failure: str | None = None
        if process_tree is not None:
            if process_tree.close():
                # KILL_ON_JOB_CLOSE should stop every assigned process. If it does
                # not, the direct-process fallback below handles the launcher.
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
            else:
                failure = "Could not close the Windows job object cleanly."

        if process_tree is None or failure is not None or process.poll() is None:
            taskkill = _taskkill_path()
            if taskkill is None:
                failure = failure or (
                    "taskkill.exe was not found; "
                    "descendant processes could not be targeted."
                )
            else:
                try:
                    result = subprocess.run(
                        [taskkill, "/PID", str(process.pid), "/T", "/F"],
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        check=False,
                        timeout=FORCED_EXIT_TIMEOUT_SECONDS,
                    )
                    if result.returncode != 0:
                        failure = (
                            failure
                            or f"taskkill.exe exited with status {result.returncode}."
                        )
                except (OSError, subprocess.TimeoutExpired) as exc:
                    failure = failure or f"Could not run taskkill.exe: {exc}"

        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
        try:
            process.wait(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise AppServerError(
                "Codex App Server did not exit after forced termination."
            ) from exc
        if failure is not None:
            raise AppServerError(
                f"Could not ensure the Codex App Server process tree stopped: {failure}"
            )
        return

    failure = None
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except OSError as exc:
        failure = f"Could not kill Codex App Server process group: {exc}"
        if process.poll() is None:
            with contextlib.suppress(OSError):
                process.kill()
    try:
        process.wait(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()
        try:
            process.wait(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as final_exc:
            raise AppServerError(
                "Codex App Server did not exit after forced termination."
            ) from final_exc
    if failure is not None:
        raise AppServerError(failure)


def _close_reader_stream(stream: TextIO, reader: threading.Thread) -> None:
    reader.join(timeout=READER_JOIN_TIMEOUT_SECONDS)
    if not reader.is_alive():
        with contextlib.suppress(OSError):
            stream.close()


def _stop_process(
    process: subprocess.Popen[str],
    stdout_thread: threading.Thread,
    stderr_thread: threading.Thread,
    process_tree: _WindowsJob | None,
) -> None:
    if process.stdin is not None:
        with contextlib.suppress(OSError):
            process.stdin.close()

    cleanup_failure: AppServerError | None = None
    graceful_exit = True
    try:
        process.wait(timeout=GRACEFUL_EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        graceful_exit = False

    if process_tree is not None:
        try:
            _force_terminate_tree(process, process_tree)
        except AppServerError as exc:
            cleanup_failure = exc
    elif not graceful_exit:
        try:
            _force_terminate_tree(process)
        except AppServerError as exc:
            cleanup_failure = exc

    # A descendant can inherit a pipe after the App Server exits. Give readers a
    # short chance to observe EOF; if they cannot, terminate the process tree too.
    stdout_thread.join(timeout=READER_JOIN_TIMEOUT_SECONDS)
    stderr_thread.join(timeout=READER_JOIN_TIMEOUT_SECONDS)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        try:
            # Retry without the Job Object even if it was closed successfully:
            # a shim child may have started before its parent joined the job.
            _force_terminate_tree(process)
        except AppServerError as exc:
            if cleanup_failure is None:
                cleanup_failure = exc
            else:
                cleanup_failure = AppServerError(f"{cleanup_failure}; {exc}")
        stdout_thread.join(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
        stderr_thread.join(timeout=FORCED_EXIT_TIMEOUT_SECONDS)

    _close_reader_stream(process.stdout, stdout_thread)
    _close_reader_stream(process.stderr, stderr_thread)
    if stdout_thread.is_alive() or stderr_thread.is_alive():
        reader_failure = AppServerError(
            "Codex App Server output readers did not stop after process cleanup."
        )
        if cleanup_failure is None:
            cleanup_failure = reader_failure
        else:
            cleanup_failure = AppServerError(f"{cleanup_failure}; {reader_failure}")
    if process.poll() is None:
        process_failure = AppServerError(
            "Codex App Server remained alive after process cleanup."
        )
        if cleanup_failure is None:
            cleanup_failure = process_failure
        else:
            cleanup_failure = AppServerError(f"{cleanup_failure}; {process_failure}")
    if cleanup_failure is not None:
        raise cleanup_failure


def _utc_iso(timestamp: float) -> str:
    try:
        moment = datetime.fromtimestamp(timestamp, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise AppServerError("App Server returned an invalid reset timestamp.") from exc
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _normalize_limit_bucket(bucket: dict[str, Any]) -> dict[str, Any]:
    used_value = bucket.get("usedPercent")
    used_percent: float | None
    if used_value is None:
        used_percent = None
    elif isinstance(used_value, bool) or not isinstance(used_value, (int, float)):
        raise AppServerError("App Server returned a non-numeric usedPercent value.")
    else:
        used_percent = float(used_value)
        if not math.isfinite(used_percent) or not 0 <= used_percent <= 100:
            raise AppServerError(
                "App Server returned usedPercent outside the range 0 through 100."
            )

    resets_value = bucket.get("resetsAt")
    if resets_value is None:
        resets_at = None
    elif isinstance(resets_value, bool) or not isinstance(resets_value, (int, float)):
        raise AppServerError("App Server returned a non-numeric resetsAt value.")
    else:
        reset_timestamp = float(resets_value)
        if not math.isfinite(reset_timestamp):
            raise AppServerError("App Server returned an invalid reset timestamp.")
        resets_at = _utc_iso(reset_timestamp)

    return {
        "usedPercent": used_percent,
        "remainingPercent": None if used_percent is None else 100 - used_percent,
        "resetsAt": resets_at,
    }


def _normalize_individual_limit(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppServerError("App Server returned a malformed individualLimit value.")

    normalized: dict[str, Any] = {}
    for field in ("limit", "used"):
        amount = value.get(field)
        if amount is not None and not isinstance(amount, str):
            raise AppServerError(
                f"App Server returned a non-string individualLimit {field} value."
            )
        normalized[field] = amount

    remaining_value = value.get("remainingPercent")
    if remaining_value is None:
        remaining_percent = None
    elif isinstance(remaining_value, bool) or not isinstance(remaining_value, int):
        raise AppServerError(
            "App Server returned a non-integer individualLimit remainingPercent value."
        )
    else:
        remaining_percent = remaining_value
        if not 0 <= remaining_percent <= 100:
            raise AppServerError(
                "App Server returned individualLimit remainingPercent outside the "
                "range 0 through 100."
            )

    resets_value = value.get("resetsAt")
    if resets_value is None:
        resets_at = None
    elif isinstance(resets_value, bool) or not isinstance(resets_value, (int, float)):
        raise AppServerError(
            "App Server returned a non-numeric individualLimit resetsAt value."
        )
    else:
        try:
            reset_timestamp = float(resets_value)
        except (OverflowError, ValueError) as exc:
            raise AppServerError(
                "App Server returned an invalid reset timestamp."
            ) from exc
        if not math.isfinite(reset_timestamp):
            raise AppServerError("App Server returned an invalid reset timestamp.")
        resets_at = _utc_iso(reset_timestamp)

    normalized["remainingPercent"] = remaining_percent
    normalized["resetsAt"] = resets_at
    return normalized


def _select_rate_limits(result: dict[str, Any]) -> dict[str, Any] | None:
    by_limit_id = result.get("rateLimitsByLimitId")
    if by_limit_id is not None:
        if not isinstance(by_limit_id, dict):
            raise AppServerError(
                "App Server returned a malformed rateLimitsByLimitId value."
            )
        codex_limits = by_limit_id.get("codex")
        if codex_limits is None:
            return None
        if not isinstance(codex_limits, dict):
            raise AppServerError(
                "App Server returned malformed limits for the codex bucket."
            )
        explicit_id = codex_limits.get("limitId")
        return codex_limits if explicit_id in (None, "codex") else None

    legacy_limits = result.get("rateLimits")
    if legacy_limits is None:
        return None
    if not isinstance(legacy_limits, dict):
        raise AppServerError("App Server returned a malformed rateLimits value.")
    explicit_id = legacy_limits.get("limitId")
    if explicit_id is not None and explicit_id != "codex":
        return None
    return legacy_limits


def _status_from_result(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise AppServerError("App Server returned a non-object rate limit result.")

    limits = _select_rate_limits(result)
    grouped: dict[str, dict[str, Any]] = {}
    individual_limit = None
    if limits is not None:
        individual_limit = _normalize_individual_limit(limits.get("individualLimit"))
        for bucket in limits.values():
            if not isinstance(bucket, dict):
                continue
            duration = bucket.get("windowDurationMins")
            if isinstance(duration, bool) or not isinstance(duration, (int, float)):
                continue
            if duration == 300:
                name = "fiveHour"
            elif duration == 10080:
                name = "weekly"
            else:
                continue
            if name in grouped:
                raise AppServerError(
                    f"App Server returned multiple {name} rate limit windows."
                )
            grouped[name] = _normalize_limit_bucket(bucket)

    return {
        "schemaVersion": 1,
        "source": "live",
        "collectedAt": datetime.now(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z"),
        "fiveHour": grouped.get("fiveHour"),
        "weekly": grouped.get("weekly"),
        "individualLimit": individual_limit,
    }


def get_live_status() -> dict[str, Any]:
    codex_command, child_env = _resolve_codex_command()
    command = (
        _windows_launcher_command(codex_command) if os.name == "nt" else codex_command
    )
    process_tree = _WindowsJob.create()
    if os.name == "nt" and process_tree is None:
        raise AppServerError(
            "Could not create a Windows job object for Codex App Server cleanup."
        )
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env,
            start_new_session=os.name != "nt",
        )
    except OSError as exc:
        if process_tree is not None:
            process_tree.close()
        raise AppServerError(
            f"Could not start 'codex app-server --stdio': {exc}"
        ) from exc

    if process_tree is not None and not process_tree.assign(process.pid):
        cleanup_failure: AppServerError | None = None
        try:
            process.kill()
        except OSError:
            pass
        finally:
            if process.stdin is not None:
                with contextlib.suppress(OSError):
                    process.stdin.close()
        try:
            process.wait(timeout=FORCED_EXIT_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            cleanup_failure = AppServerError(
                f"Could not stop the unassigned Windows Codex launcher: {exc}"
            )
        finally:
            process_tree.close()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    with contextlib.suppress(OSError):
                        stream.close()
        message = "Could not assign the Windows Codex launcher to a job object."
        if cleanup_failure is not None:
            raise AppServerError(f"{message} {cleanup_failure}") from cleanup_failure
        raise AppServerError(message)

    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    events: queue.Queue[tuple[str, Any]] = queue.Queue()
    stderr_tail = _StderrTail(STDERR_TAIL_LIMIT)
    stdout_thread = threading.Thread(
        target=_read_stdout,
        args=(process.stdout, events),
        name="codex-app-server-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_drain_stderr,
        args=(process.stderr, stderr_tail),
        name="codex-app-server-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    status: dict[str, Any] | None = None
    failure: AppServerError | None = None
    try:
        if os.name == "nt":
            _start_windows_launcher(process.stdin)
        _send_message(
            process.stdin,
            {
                "method": "initialize",
                "id": 0,
                "params": {
                    "clientInfo": {
                        "name": "codex-usage",
                        "title": "Codex Usage",
                        "version": "0.1.0",
                    }
                },
            },
        )
        _receive_response(events, process, 0)
        _send_message(process.stdin, {"method": "initialized"})
        _send_message(process.stdin, {"method": "account/rateLimits/read", "id": 1})
        response = _receive_response(events, process, 1)
        status = _status_from_result(response["result"])
    except AppServerError as exc:
        failure = exc
    finally:
        try:
            _stop_process(process, stdout_thread, stderr_thread, process_tree)
        except AppServerError as exc:
            if failure is None:
                failure = exc
            else:
                failure = AppServerError(f"{failure}\nCleanup failed: {exc}")

    if failure is not None:
        details = stderr_tail.get()
        if details:
            raise AppServerError(
                f"{failure}\nCodex App Server stderr:\n{details}"
            ) from failure
        raise failure
    assert status is not None
    return status
