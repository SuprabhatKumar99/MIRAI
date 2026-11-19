"""Export standalone Harbor 0.25 task packages using verified Phase 1–4 artifacts."""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import tomllib
import zipfile

import tomli_w
from packaging.specifiers import SpecifierSet
from packaging.utils import canonicalize_name, parse_wheel_filename

from app.schemas.harbor import (
    HarborExportReport, HarborExportRequest, HarborMetadataConfig, HarborTaskInfo, HarborTaskManifest,
)
from app.services.harbor_guest import digest, ordinary, tree
from app.services.isolated_validation import _fingerprint, read_recorded_validation
from app.services.native_test_generation import recheck_native_suite
from app.services.regression_protection import TEST_DIRECTORIES, protect_regression_paths
from app.services.sandbox_dependencies import configured_bundle, verify_project_dependencies
from app.services.snapshot_service import matching_snapshot_id, restore_snapshot
from app.services.solution_changes import apply_changes, check_change_paths, has_secret_marker, is_protected_path, solution_path_parts
from app.services.validation_service import prepare_test_files_safely

PYTHON_IMAGE_ID = "sha256:07d4824036af339fa1b6ef769cc99d9c10a141769538b52b3b597c84f7a7b535"
PYTHON_IMAGE = f"docker.io/library/python@{PYTHON_IMAGE_ID}"
PYTHON_VERSION = "3.11.18"
MAX_ARCHIVE_SIZE_BYTES = 224 * 1024 * 1024
GUEST = Path(__file__).with_name("harbor_guest.py")
NATIVE_GUEST = Path(__file__).with_name("sandbox_guest.py")
PRIVATE_SUFFIXES = {".pyc", ".pyo", ".exe", ".dll"}
OFFLINE_PIP = (
    "python -I -m pip --isolated --disable-pip-version-check install --no-index "
    "--find-links=/opt/ctf/wheels --require-hashes --only-binary=:all: --no-deps --no-compile "
    "--no-cache-dir --target=/opt/ctf/site -r /opt/ctf/requirements.lock"
)


def _name_parts(task_prefix: str, task_id: str) -> tuple[str, str]:
    prefix = re.sub(r"[^a-z0-9-]", "-", task_prefix.strip().lower())[:40].strip("-") or "ctf"
    identifier = re.sub(r"[^a-z0-9_-]", "-", task_id.strip().lower())[:85].strip("-_") or "task"
    return prefix, identifier


def sanitize_task_dirname(task_prefix: str, task_id: str) -> str:
    return "-".join(_name_parts(task_prefix, task_id))


def validate_export_file_path(relative_path: str) -> Path:
    parts = solution_path_parts(relative_path)
    if any(part.casefold() in {"venv", ".venv", "__pycache__", "node_modules"} for part in parts):
        raise ValueError("Forbidden cache or dependency path in an exported change.")
    if Path(parts[-1]).suffix.casefold() in PRIVATE_SUFFIXES:
        raise ValueError("Forbidden executable or compiled file in an exported change.")
    return Path(*parts)


def generate_instruction_markdown(task, project_workdir: str = "/app") -> str:
    requirements = "\n".join(f"- {text}" for text in task.requirements)
    criteria = "\n".join(f"- {text}" for text in task.acceptance_criteria)
    text = (
        f"# {task.title}\n\n{task.description}\n\n"
        f"Implement the change in the provided repository at {project_workdir}. "
        "Edit the repository in place and preserve the existing tests and fixtures.\n\n"
        f"## Requirements\n\n{requirements}\n\n## Acceptance criteria\n\n{criteria}\n"
    )
    if len(text.encode("utf-8")) > 64 * 1024 or has_secret_marker(text):
        raise ValueError("Task instructions exceed the package limit or contain credential-like content.")
    return text


def generate_task_toml(manifest: HarborTaskManifest) -> str:
    return tomli_w.dumps(manifest.model_dump(mode="json", exclude_none=True))


def _dependency_instructions(dependencies: bool) -> str:
    if not dependencies:
        return ""
    return (
        "COPY requirements.lock /opt/ctf/requirements.lock\n"
        "COPY wheels/ /opt/ctf/wheels/\n"
        f"RUN --network=none {OFFLINE_PIP}\n"
        "ENV PYTHONPATH=/opt/ctf/site\n"
    )


def generate_environment_dockerfile(manifest_sha: str, dependencies: bool = False) -> str:
    return (
        f"FROM {PYTHON_IMAGE}\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
        "COPY source/ /app/\n"
        "COPY snapshot.json source-check.py /opt/ctf/\n"
        f'RUN --network=none ["python", "-I", "-B", "/opt/ctf/source-check.py", "source", '
        f'"/opt/ctf/snapshot.json", "{manifest_sha}"]\n'
        f"{_dependency_instructions(dependencies)}WORKDIR /app\n"
    )


def _verifier_dockerfile(dependencies: bool) -> str:
    return (
        f"FROM {PYTHON_IMAGE}\n"
        "ENV PYTHONDONTWRITEBYTECODE=1\n"
        f"{_dependency_instructions(dependencies)}"
        "COPY --chmod=755 test.sh /tests/test.sh\n"
        "COPY verify.py sandbox_guest.py plan.json /tests/\n"
        "COPY private/ /tests/private/\n"
        "WORKDIR /\n"
    )


def _docker_compose(*, verifier: bool = False) -> str:
    capabilities = "    cap_add: [SETGID, SETUID]\n" if verifier else ""
    return (
        "services:\n"
        "  main:\n"
        '    network_mode: "none"\n'
        '    security_opt: ["no-new-privileges:true"]\n'
        "    pids_limit: 64\n"
        "    cap_drop: [ALL]\n"
        f"{capabilities}"
    )


def generate_verifier_test_sh(plan_sha: str) -> str:
    return (
        "#!/bin/sh\nset -eu\ncd /\n"
        f'exec /usr/local/bin/python -I -B /tests/verify.py verify /tests "{plan_sha}"\n'
    )


def generate_oracle_solve_sh(changes_sha: str) -> str:
    return (
        "#!/bin/sh\nset -eu\ncd /app\n"
        f'exec /usr/local/bin/python -I -B /solution/apply.py oracle /solution "{changes_sha}"\n'
    )


def _write(root: Path, name: str, data: bytes | str) -> None:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("xb") as output:
        output.write(data.encode("utf-8") if isinstance(data, str) else data)
    if name in {"tests/test.sh", "solution/solve.sh"}:
        target.chmod(0o755)


def _write_json(root: Path, name: str, data) -> str:
    encoded = (json.dumps(data, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n").encode()
    _write(root, name, encoded)
    return hashlib.sha256(encoded).hexdigest()


def _record(request, snapshot, bundle):
    claim = request.validation_report
    identifier = request.validation_id
    if claim and (claim.overall_verdict == "VALIDATED" or claim.evidence_location):
        identifier = identifier or claim.id
    if not identifier:
        return None
    record = read_recorded_validation(identifier, request.task, request.solution, request.test_suite)
    if claim is not None and claim != record:
        raise ValueError("The supplied validation report differs from the trusted server record.")
    if (record.snapshot_id, record.snapshot_sha256, record.starting_commit_sha) != (
        snapshot.snapshot_id, snapshot.content_sha256, snapshot.commit_sha,
    ):
        raise ValueError("The validation record does not match the starting snapshot.")
    if record.overall_verdict == "VALIDATED" and (
        record.dependency_lock_sha256 != bundle.digest or record.sandbox_image_id != PYTHON_IMAGE_ID
    ):
        raise ValueError("Validation used a different runtime or dependency lock. Revalidate for this package.")
    return record


def _copy_original(root, stage):
    directories = []
    files = tree(root, omit_git=True, directories=directories)
    for directory in directories:
        if is_protected_path((*directory.split("/"), "asset.txt")):
            raise ValueError("A starting snapshot directory occupies a protected credential path.")
        (stage / "environment" / "source" / directory).mkdir(parents=True, exist_ok=True)
    signatures = {}
    for path, original in files.items():
        parts = tuple(path.split("/"))
        if "__pycache__" in (part.casefold() for part in parts) or original.suffix.casefold() in {".pyc", ".pyo"}:
            raise ValueError("Harbor packaging requires a starting snapshot free of compiled Python bytecode.")
        if parts[-1].casefold() in {".env.example", ".env.sample"}:
            parts = (*parts[:-1], "env-template.txt")
        if is_protected_path(parts):
            raise ValueError("A starting snapshot file occupies a protected credential path.")
        data = original.read_bytes()
        if has_secret_marker(data.decode("utf-8", "ignore")):
            raise ValueError("A starting snapshot file contains credential-like material.")
        _write(stage, f"environment/source/{path}", data)
        signatures[path] = hashlib.sha256(data).hexdigest()
    return signatures, sorted(directories)


def _original_inputs(project, signatures):
    inputs = {item.path for item in project.original_tests}
    for name in signatures:
        path = Path(name)
        if any(part.casefold() in TEST_DIRECTORIES for part in path.parts[:-1]):
            inputs.add(name)
        if path.name.casefold() in {"pytest.ini", ".pytest.ini", "conftest.py"}:
            inputs.add(name)
        if any(name.startswith(directory.rstrip("/") + "/") for directory in project.directories):
            inputs.add(name)
    if project.config:
        inputs.add(project.config)
    return {name: signatures[name] for name in inputs if name in signatures}


def _line_ending_recovery(stage, inputs):
    """Record Harbor 0.25's Windows upload transform and retain the exact source bytes."""
    recovery = {}
    for name in inputs:
        if not name.endswith((".py", ".sh", ".ps1", ".cmd", ".bat")):
            continue
        data = (stage / "environment" / "source" / name).read_bytes()
        normalized = data.replace(b"\r\n", b"\n").removesuffix(b"\r")
        if normalized != data:
            recovery[name] = hashlib.sha256(normalized).hexdigest()
            _write(stage, f"tests/private/original/{name}", data)
    return recovery


def _test_group(stage, name, files, project):
    group = {"group": name, "kind": "tests", "framework": project.framework, "cwd": project.scope.as_posix(),
             "targets": [item.path for item in files], "selectors": [], "files": {}}
    if project.framework == "pytest":
        group["config"] = project.config or ""
    if name != "original_regression":
        for file in files:
            validate_export_file_path(file.path)
            _write(stage, f"tests/private/{name}/{file.path}", file.code)
            group["files"][file.path] = file.content_sha256
            group["selectors"].extend({"path": file.path, "selector": case.name} for case in file.test_cases)
    return group


def _verify_python_project(project_root, framework, bundle):
    verify_project_dependencies(project_root.resolve(), framework, bundle)
    configuration = project_root / "pyproject.toml"
    if configuration.is_file():
        manifest = tomllib.loads(configuration.read_text(encoding="utf-8"))
        requires = manifest.get("project", {}).get("requires-python")
        if requires is not None and (
            not isinstance(requires, str) or not SpecifierSet(requires).contains(PYTHON_VERSION, prereleases=True)
        ):
            raise ValueError(f"Harbor Python {PYTHON_VERSION} does not satisfy the project's requires-python.")


def _package(stage, request, root, snapshot, bundle, record):
    task, solution, suite = request.task, request.solution, request.test_suite
    if task.language.casefold() != "python" or not any(lang.casefold() == "python" for lang in snapshot.detected_languages):
        raise ValueError("Harbor packaging currently supports snapshot-backed Python projects.")
    if suite.quality_status != "structurally_checked":
        raise ValueError("Harbor packaging requires a structurally checked Phase 3 test suite.")
    project = recheck_native_suite(root, snapshot, task, solution, suite)
    if not project.original_tests or not suite.feature_test_files:
        raise ValueError("Harbor packaging requires original regression and generated feature tests.")
    project_root = root / project.scope
    _verify_python_project(project_root, project.framework, bundle)
    signatures, directories = _copy_original(root, stage)
    snapshot_sha = _write_json(stage, "environment/snapshot.json", {
        "format": 1, "files": signatures, "directories": directories, "snapshot_id": snapshot.snapshot_id,
        "archive_sha256": snapshot.content_sha256, "commit_sha": snapshot.commit_sha,
        "source_type": snapshot.source_type, "repository_identity": snapshot.repository_identity,
    })
    _write(stage, "environment/source-check.py", GUEST.read_bytes())
    if bundle.lock:
        hashes = {
            canonicalize_name(line.split("==", 1)[0]): line.split("--hash=sha256:", 1)[1]
            for line in bundle.lock.decode().splitlines()
        }
        for context in ("environment", "tests"):
            _write(stage, f"{context}/requirements.lock", bundle.lock)
        for wheel in bundle.wheels:
            ordinary(wheel)
            content = wheel.read_bytes()
            distribution, _, _, _ = parse_wheel_filename(wheel.name)
            if hashlib.sha256(content).hexdigest() != hashes[distribution]:
                raise ValueError("An approved wheel changed while the package was prepared.")
            for context in ("environment", "tests"):
                _write(stage, f"{context}/wheels/{wheel.name}", content)
    _write(stage, "environment/Dockerfile", generate_environment_dockerfile(snapshot_sha, bool(bundle.lock)))
    _write(stage, "environment/docker-compose.yaml", _docker_compose())

    protect_regression_paths(root, snapshot.existing_test_files, [file.path for file in solution.files],
                             {file.path: file.code for file in solution.files})
    authorized = frozenset(record.deleted_files) if record and record.overall_verdict == "VALIDATED" else frozenset()
    if not solution.files or any(
        (file.status != "create" and not file.base_sha256)
        or (file.status != "delete" and not file.result_sha256)
        for file in solution.files
    ):
        raise ValueError("The oracle requires a Phase 2 change set with complete file checksums.")
    changed = apply_changes(root, solution.files, authorized_deletions=authorized, check_content=True)
    _verify_python_project(project_root, project.framework, bundle)
    prepare_test_files_safely(root, suite)
    changes = []
    for index, file in enumerate(changed):
        payload = None
        if file.status != "delete":
            # Harbor normalizes script and Python line endings during Windows uploads.
            payload = f"{index:03d}.blob"
            _write(stage, f"solution/files/{payload}", file.code)
        changes.append({"path": file.path, "before": file.base_sha256, "after": file.result_sha256,
                        "payload": payload})
    change_sha = _write_json(stage, "solution/changes.json", changes)
    _write(stage, "solution/apply.py", GUEST.read_bytes())
    _write(stage, "solution/solve.sh", generate_oracle_solve_sh(change_sha))

    groups = [
        {"group": "build", "kind": "compile", "framework": project.framework,
         "cwd": project.scope.as_posix(), "targets": [], "selectors": [], "files": {}},
        _test_group(stage, "original_regression", project.original_tests, project),
        _test_group(stage, "feature", suite.feature_test_files, project),
    ]
    if suite.regression_test_files:
        groups.append(_test_group(stage, "regression", suite.regression_test_files, project))
    original_inputs = _original_inputs(project, signatures)
    plan_sha = _write_json(stage, "tests/plan.json", {
        "groups": groups, "original_inputs": original_inputs, "original_paths": sorted(signatures),
        "line_ending_recovery": _line_ending_recovery(stage, original_inputs),
    })
    _write(stage, "tests/verify.py", GUEST.read_bytes())
    _write(stage, "tests/sandbox_guest.py", NATIVE_GUEST.read_bytes())
    _write(stage, "tests/test.sh", generate_verifier_test_sh(plan_sha))
    _write(stage, "tests/Dockerfile", _verifier_dockerfile(bool(bundle.lock)))
    _write(stage, "tests/docker-compose.yaml", _docker_compose(verifier=True))

    prefix, task_id = _name_parts(request.task_prefix, task.id)
    manifest = HarborTaskManifest(
        task=HarborTaskInfo(name=f"{prefix}/{task_id}", description=task.title[:200]),
        metadata=HarborMetadataConfig(
            snapshot_id=snapshot.snapshot_id, content_sha256=snapshot.content_sha256,
            starting_commit_sha=snapshot.commit_sha, source_manifest_sha256=snapshot_sha,
            validation_status=record.overall_verdict if record else "NOT_VALIDATED",
            validation_id=record.id if record else None, dependency_lock_sha256=bundle.digest,
            task_sha256=_fingerprint(task), solution_sha256=_fingerprint(solution), test_suite_sha256=_fingerprint(suite),
            difficulty=task.difficulty, category=task.category, tags=["python", project.framework],
            project_name=snapshot.project_name,
        ),
    )
    workdir = "/app" if project.scope.as_posix() == "." else f"/app/{project.scope.as_posix()}"
    _write(stage, "instruction.md", generate_instruction_markdown(task, workdir))
    _write(stage, "task.toml", generate_task_toml(manifest))
    return manifest


def export_harbor_task_package(request: HarborExportRequest, output_dir: Path | str | None = None):
    """Create one native task directory and ZIP without running uploaded code."""
    task, solution, suite = request.task, request.solution, request.test_suite
    check_change_paths(solution.files)
    for file in (*solution.files, *suite.feature_test_files, *suite.regression_test_files):
        validate_export_file_path(file.path)
    snapshot_id = matching_snapshot_id(task.snapshot_id, solution.snapshot_id, suite.repository.snapshot_id)
    if not snapshot_id or {task.snapshot_id, solution.snapshot_id, suite.repository.snapshot_id} != {snapshot_id}:
        raise ValueError("Harbor packaging requires one immutable snapshot shared by all artifacts.")
    bundle = configured_bundle(os.getenv("CTF_VALIDATION_WHEELHOUSE"))
    name = sanitize_task_dirname(request.task_prefix, task.id)
    parent = Path(output_dir).resolve() if output_dir else Path(tempfile.mkdtemp(prefix="ctf_harbor_"))
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / name
    archive_path = parent / f"{name}.zip"
    if stage.exists() or archive_path.exists() or stage.is_symlink() or archive_path.is_symlink():
        raise ValueError("The target Harbor package already exists.")
    stage.mkdir()
    archive_created = False
    try:
        with restore_snapshot(snapshot_id) as (root, snapshot_record):
            snapshot = snapshot_record.analysis
            record = _record(request, snapshot, bundle)
            manifest = _package(stage, request, root, snapshot, bundle, record)
        exported = sorted(path.relative_to(stage).as_posix() for path in stage.rglob("*") if path.is_file())
        with zipfile.ZipFile(archive_path, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=3) as archive:
            archive_created = True
            for empty in sorted(path for path in stage.rglob("*") if path.is_dir() and not any(path.iterdir())):
                entry = zipfile.ZipInfo(f"{name}/{empty.relative_to(stage).as_posix()}/")
                entry.create_system = 3
                entry.external_attr = ((stat.S_IFDIR | 0o755) << 16) | 0x10
                archive.writestr(entry, b"")
            for relative in exported:
                path = stage / relative
                ordinary(path)
                entry = zipfile.ZipInfo(f"{name}/{relative}")
                entry.create_system = 3
                mode = 0o755 if relative in {"tests/test.sh", "solution/solve.sh"} else 0o644
                entry.external_attr = (stat.S_IFREG | mode) << 16
                entry.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as source, archive.open(entry, "w") as target:
                    shutil.copyfileobj(source, target, 65536)
        size = archive_path.stat().st_size
        if size > MAX_ARCHIVE_SIZE_BYTES:
            raise ValueError("Harbor archive exceeds the package size limit.")
        return archive_path, HarborExportReport(
            task_id=task.id, snapshot_id=snapshot_id, snapshot_sha256=snapshot.content_sha256,
            validation_id=record.id if record else None, harbor_task_name=manifest.task.name,
            export_directory=str(stage), archive_path=str(archive_path), files_exported=exported,
            archive_size_bytes=size, validation_status_recorded=manifest.metadata.validation_status,
            exported_at=datetime.now(timezone.utc).isoformat(),
        )
    except Exception:
        shutil.rmtree(stage)
        if archive_created:
            archive_path.unlink()
        raise
