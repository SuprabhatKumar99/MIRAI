"""Local Docker control plane. Workloads receive only read-only Docker volumes."""

from abc import ABC, abstractmethod
from datetime import datetime
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
from dataclasses import dataclass

from pydantic import ValidationError

from app.schemas.validation import CaseOutcome, CommandEvidence, FailureCategory, SandboxRun
from app.services.sandbox_dependencies import DependencyBundle
from app.services.sandbox_guest import INSTALL_ARGV, MAX_CASES
from app.services.solution_changes import has_secret_marker, is_protected_path, solution_path_parts

DEFAULT_TIMEOUT = 45
JOB_TIMEOUT = 240
MEMORY = "512m"
PIDS = 64
MAX_REPOSITORY_BYTES = 108 * 1024 * 1024
MAX_BUNDLE_BYTES = 205 * 1024 * 1024
MAX_FILES = 1800
PYTHON = "/usr/local/bin/python"
GUEST_ARGV = [PYTHON, "-I", "-B", "/bundle/runner.py"]
WATCHDOG = "/usr/bin/timeout"
_IMAGE = re.compile(r"sha256:[0-9a-f]{64}")

_STAGE = r"""import pathlib, shutil, sys, tarfile
root = pathlib.Path('/bundle')
size = count = 0
with tarfile.open(fileobj=sys.stdin.buffer, mode='r|') as archive:
    for item in archive:
        parts = item.name.split('/')
        if any(part in ('', '.', '..') or '\\' in part for part in parts) or not item.isfile():
            raise ValueError('Unsafe staging member')
        count += 1
        size += item.size
        if count > 2000 or size > 205 * 1024 * 1024:
            raise ValueError('Staging limit exceeded')
        target = root.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(item) as source, target.open('xb') as output:
            shutil.copyfileobj(source, output, 65536)
        target.chmod(0o444)
"""


class SandboxError(RuntimeError):
    def __init__(self, category: FailureCategory, message: str):
        super().__init__(message)
        self.category = category


@dataclass(frozen=True)
class SandboxPlan:
    kind: str
    cwd: str
    framework: str
    targets: tuple[str, ...] = ()
    selectors: tuple[tuple[str, str], ...] = ()

    def data(self):
        return {
            "kind": self.kind, "cwd": self.cwd, "framework": self.framework, "targets": self.targets,
            "selectors": [{"path": path, "selector": selector} for path, selector in self.selectors],
        }


class ValidationExecutionBackend(ABC):
    image_id: str | None = None

    @abstractmethod
    def probe(self) -> None:
        """Establish the isolation capability before accepting work."""

    @abstractmethod
    def run(self, workspace: Path, plan: SandboxPlan, dependencies: DependencyBundle,
            log_path: Path) -> SandboxRun:
        """Carry out a bounded command with actual process evidence."""


@dataclass
class _CLIResult:
    code: int | None
    stdout: bytes
    stderr: bytes
    timed_out: bool
    truncated: bool


def _docker_env() -> dict[str, str]:
    names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR"}
    return {key: value for key, value in os.environ.items() if key.upper() in names}


def _cli(command: list[str], timeout: float, stdin=None, limit: int = 1024 * 1024) -> _CLIResult:
    try:
        with subprocess.Popen(command, env=_docker_env(), stdin=stdin if stdin is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            buffers = [bytearray(), bytearray()]
            truncated = [False, False]

            def consume(pipe, index):
                while data := pipe.read(8192):
                    room = limit - len(buffers[index])
                    buffers[index].extend(data[:room])
                    truncated[index] |= len(data) > room

            readers = [threading.Thread(target=consume, args=(pipe, index), daemon=True)
                       for index, pipe in enumerate((process.stdout, process.stderr))]
            for reader in readers:
                reader.start()
            timed_out = False
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                process.kill()
                process.wait()
            for reader in readers:
                reader.join()
            return _CLIResult(None if timed_out else process.returncode, bytes(buffers[0]),
                              bytes(buffers[1]), timed_out, any(truncated))
    except OSError as exc:
        raise SandboxError("infrastructure_unavailable", "The local Docker CLI could not be started.") from exc


def _regular(info: os.stat_result) -> bool:
    return not stat.S_ISLNK(info.st_mode) and not bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _add_bytes(archive, name: str, content: bytes) -> None:
    member = tarfile.TarInfo(name)
    member.size = len(content)
    member.mode = 0o444
    archive.addfile(member, io.BytesIO(content))


def write_bundle(stream, workspace: Path, plan: SandboxPlan, dependencies: DependencyBundle) -> None:
    if not workspace.is_dir() or not _regular(workspace.lstat()):
        raise SandboxError("security_policy", "The workspace is not an ordinary directory.")
    size = count = 0
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for base, dirs, paths in os.walk(workspace):
            directory = Path(base)
            for name in sorted(dirs + paths):
                path = directory / name
                if not _regular(path.lstat()):
                    raise SandboxError("security_policy", "Links and reparse points are forbidden in sandbox input.")
            dirs[:] = sorted(name for name in dirs if name.casefold() != ".git")
            for name in sorted(paths):
                path = directory / name
                relative = path.relative_to(workspace).as_posix()
                if any(part.casefold() == ".git" for part in path.relative_to(workspace).parts):
                    continue
                info = path.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise SandboxError("security_policy", "Sandbox input must contain regular, unlinked files.")
                parts = tuple(relative.split("/"))
                if name.lower() in (".env.example", ".env.sample"):
                    parts = (*parts[:-1], "env-template.txt")
                if is_protected_path(parts):
                    raise SandboxError("security_policy", "The snapshot contains a credential path.")
                try:
                    solution_path_parts("/".join(parts))
                except ValueError as exc:
                    raise SandboxError("security_policy", "A workspace path is unsafe.") from exc
                size += info.st_size
                count += 1
                if size > MAX_REPOSITORY_BYTES or count > MAX_FILES or info.st_size > 10 * 1024 * 1024:
                    raise SandboxError("resource_failure", "The sandbox repository bundle exceeds its bounds.")
                content = path.read_bytes()
                if len(content) != info.st_size or has_secret_marker(content.decode("utf-8", errors="ignore")):
                    raise SandboxError("security_policy", "A workspace file changed or contains credential-like material.")
                _add_bytes(archive, f"repo/{relative}", content)
        _add_bytes(archive, "plan.json", json.dumps(plan.data()).encode())
        _add_bytes(archive, "runner.py", Path(__file__).with_name("sandbox_guest.py").read_bytes())
        if dependencies.lock:
            _add_bytes(archive, "requirements.lock", dependencies.lock)
            for wheel in dependencies.wheels:
                if not _regular(wheel.lstat()) or not wheel.is_file():
                    raise SandboxError("dependency_policy", "An approved wheel became unavailable.")
                size += wheel.stat().st_size
                if size > MAX_BUNDLE_BYTES:
                    raise SandboxError("resource_failure", "The staging bundle exceeds its storage limit.")
                with wheel.open("rb") as source:
                    member = tarfile.TarInfo("wheels/" + wheel.name)
                    member.size = wheel.stat().st_size
                    member.mode = 0o444
                    archive.addfile(member, source)
    if stream.tell() > MAX_BUNDLE_BYTES:
        raise SandboxError("resource_failure", "The staging archive exceeds its storage limit.")


def _flags(name: str, user: str, entrypoint: str) -> list[str]:
    return [
        "run", "--name", name, "--hostname", "ctf-sandbox", "--pull", "never", "--network", "none", "--ipc", "none",
        "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
        "--cpus", "1", "--memory", MEMORY, "--memory-swap", MEMORY,
        "--pids-limit", str(PIDS), "--ulimit", "nofile=256:256", "--ulimit", "fsize=33554432:33554432",
        "--log-driver", "none", "--no-healthcheck", "--user", user,
        "--entrypoint", entrypoint,
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=48m,mode=1777",
        "--tmpfs", "/work:rw,nosuid,nodev,exec,size=224m,uid=65534,gid=65534,mode=0700",
    ]


def _docker_duration_ms(state: dict) -> int:
    start, end = state.get("StartedAt"), state.get("FinishedAt")
    if not start or not end or end.startswith("0001-"):
        return 0
    return max(0, round((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() * 1000))


class DockerExecutionBackend(ValidationExecutionBackend):
    def __init__(self, image_id: str, job_id: str, config_dir: Path, *, timeout: int = DEFAULT_TIMEOUT):
        self.image_id = image_id
        self.timeout = timeout
        self.job_id = job_id
        self.sequence = 0
        self.deadline = time.monotonic() + JOB_TIMEOUT
        docker = shutil.which("docker")
        self.executable = str(Path(docker).resolve()) if docker else None
        self.host = "npipe:////./pipe/dockerDesktopLinuxEngine" if os.name == "nt" else "unix:///var/run/docker.sock"
        self.config_dir = config_dir

    def _call(self, arguments: list[str], timeout: float, stdin=None, *, cleanup=False) -> _CLIResult:
        if not self.executable:
            raise SandboxError("infrastructure_unavailable", "The local Docker CLI is unavailable.")
        remaining = timeout if cleanup else min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            return _CLIResult(None, b"", b"", True, False)
        return _cli([self.executable, "--config", str(self.config_dir), "--host", self.host, *arguments],
                    remaining, stdin)

    def probe(self) -> None:
        if not _IMAGE.fullmatch(self.image_id):
            raise SandboxError("security_policy", "Configure an approved local Docker image by its full image ID.")
        info = self._call(["info", "--format",
                           "{{.OSType}}|{{.MemoryLimit}}|{{.SwapLimit}}|{{.PidsLimit}}|{{.CPUCfsQuota}}|{{json .SecurityOptions}}"], 8)
        if info.code != 0:
            raise SandboxError("infrastructure_unavailable", "The local Linux Docker engine is unavailable.")
        fields = info.stdout.decode("utf-8", "replace").strip().split("|")
        if (len(fields) != 6 or fields[:5] != ["linux", "true", "true", "true", "true"]
                or '"name=seccomp,profile=builtin"' not in fields[5]):
            raise SandboxError("security_policy", "Docker lacks required Linux memory, CPU, process, swap or seccomp controls.")
        inspect = self._call(["image", "inspect", "--format", "{{json .}}", self.image_id], 8)
        if inspect.code != 0:
            raise SandboxError("infrastructure_unavailable", "The approved local sandbox image is missing.")
        try:
            image = json.loads(inspect.stdout)
            if image["Id"] != self.image_id or image["Config"].get("Volumes"):
                raise ValueError
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise SandboxError("security_policy", "Docker cannot verify the approved image has no implicit volumes.") from exc

    def run(self, workspace: Path, plan: SandboxPlan, dependencies: DependencyBundle,
            log_path: Path) -> SandboxRun:
        self.sequence += 1
        suffix = f"ctf-validation-{self.job_id}-{self.sequence}"
        stage, work, volume = suffix + "-stage", suffix + "-work", suffix + "-input"
        started = time.monotonic()
        receipt = SandboxRun(timeout_seconds=self.timeout, log_location=f"{log_path.parent.name}/{log_path.name}")
        stdout = stderr = ""
        cleanup = []
        volume_created = False
        try:
            with tempfile.TemporaryFile(mode="w+b") as data:
                write_bundle(data, workspace, plan, dependencies)
                data.seek(0)
                created = self._call(["volume", "create", "--label", "coding-task-factory.validation=true", volume], 8)
                if created.code != 0:
                    raise SandboxError("timeout" if created.timed_out else "infrastructure_unavailable", "Docker input staging could not start.")
                volume_created = True
                cleanup.append(stage)
                prepared = self._call([
                    *_flags(stage, "0:0", PYTHON), "-i", "--mount", f"type=volume,src={volume},dst=/bundle,volume-nocopy",
                    self.image_id, "-I", "-B", "-c", _STAGE,
                ], min(self.timeout, 20), data)
            if prepared.code != 0:
                raise SandboxError("timeout" if prepared.timed_out else "infrastructure_unavailable", "Trusted Docker staging failed.")
            cleanup.append(work)
            command = [WATCHDOG, "--foreground", "--kill-after=2s", f"{self.timeout}s", *GUEST_ARGV]
            argv = _flags(work, "65534:65534", WATCHDOG) + [
                "--workdir", "/work",
                "--mount", f"type=volume,src={volume},dst=/bundle,readonly,volume-nocopy", self.image_id, *command[1:],
            ]
            result = self._call(argv, self.timeout)
            inspected = self._call(["inspect", "--format", "{{json .State}}", work], 5, cleanup=True)
            state = json.loads(inspected.stdout) if inspected.code == 0 else {}
            stdout = result.stdout.decode("utf-8", "replace")
            stderr = result.stderr.decode("utf-8", "replace")
            receipt.exit_code = state.get("ExitCode") if not state.get("Running") else None
            receipt.oom_killed = state.get("OOMKilled", False)
            receipt.timed_out = result.timed_out or receipt.exit_code == 124
            receipt.output_truncated = result.truncated
            if state.get("StartedAt") and not str(state["StartedAt"]).startswith("0001-"):
                receipt.commands = [CommandEvidence(argv=command, working_directory="/work",
                                                    exit_code=receipt.exit_code,
                                                    duration_ms=_docker_duration_ms(state))]
            if receipt.timed_out:
                receipt.failure_category = "timeout"
            elif receipt.oom_killed or receipt.exit_code in (137, 152, 153):
                receipt.failure_category = "resource_failure"
            elif not receipt.commands or state.get("Error") or result.code != receipt.exit_code:
                receipt.failure_category = "infrastructure_unavailable"
            elif result.truncated:
                receipt.failure_category = "resource_failure"
            else:
                guest = json.loads(stdout)
                if (guest["exit_code"] != receipt.exit_code or not isinstance(guest["cases"], list)
                        or len(guest["cases"]) > MAX_CASES or not isinstance(guest["commands"], list)):
                    raise ValueError("Mismatched sandbox result")
                receipt.failure_category = guest["failure_category"]
                # Revalidate the category assignment and all event shapes before publishing it.
                receipt = SandboxRun.model_validate(receipt.model_dump())
                receipt.cases = [CaseOutcome.model_validate(case) for case in guest["cases"]]
                installs = [CommandEvidence.model_validate(data) for data in guest["commands"]]
                if len(installs) != bool(dependencies.lock) or any(
                    item.argv != INSTALL_ARGV or item.working_directory != "/work" or item.exit_code is None
                    or (item.exit_code != 0) != (receipt.failure_category == "dependency_installation")
                    for item in installs
                ):
                    raise ValueError("Mismatched installation evidence")
                receipt.commands.extend(installs)
                stdout, stderr = str(guest["stdout"]), str(guest["stderr"])
                receipt.output_truncated |= bool(guest["output_truncated"])
        except SandboxError as exc:
            receipt.failure_category = exc.category
            receipt.timed_out = exc.category == "timeout"
            stderr = str(exc)
        except (KeyError, ValueError, TypeError, AttributeError, RecursionError, ValidationError):
            receipt.failure_category = "invalid_evidence"
            stderr = "Docker returned incomplete or invalid execution evidence."
        finally:
            for command in ([["rm", "--force", container] for container in reversed(cleanup)]
                            + ([["volume", "rm", volume]] if volume_created else [])):
                try:
                    removed = self._call(command, 4, cleanup=True)
                    if removed.code == 0 or b"No such container" in removed.stderr:
                        continue
                except SandboxError:
                    pass
                receipt.failure_category = receipt.failure_category or "infrastructure_unavailable"
                stderr += "\nDocker resource cleanup did not complete."
        receipt.duration_ms = round((time.monotonic() - started) * 1000)
        log_path.write_bytes((f"stdout\n{stdout}\n\nstderr\n{stderr}").encode("utf-8", "replace")[:75 * 1024])
        return receipt
