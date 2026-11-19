"""Trust boundaries for source restoration, oracle application, and Harbor verification."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import unicodedata

PYTHON = "/usr/local/bin/python"
ROOT = Path("/app")
LOGS = Path("/logs/verifier")
MAX_FILES = 1800
MAX_BYTES = 108 * 1024 * 1024
MAX_FILE = 10 * 1024 * 1024
MAX_OUTPUT = 1024 * 1024


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def safe_parts(relative):
    parts = tuple(relative.split("/"))
    if len(relative) > 1024 or len(parts) > 100 or any(
        part in {"", ".", ".."} or part.endswith((" ", ".")) or len(part.encode()) > 255
        or re.search(r'[\\<>:"|?*\x00-\x1f\x7f]', part)
        or re.fullmatch(r"CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[0-9¹²³]", part.split(".")[0], re.I)
        for part in parts
    ):
        raise ValueError("Unsafe repository path")
    return parts


def ordinary(path, *, directory=False):
    info = path.lstat()
    mask = stat.S_ISDIR if directory else stat.S_ISREG
    if (not mask(info.st_mode) or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            or (not directory and info.st_nlink != 1)):
        raise ValueError("Repository links and special files are forbidden")
    return info


def reject_walk_error(error):
    raise error


def tree(root, *, omit_git=False, omit_bytecode=False, directories=None):
    """Preflight a regular-file tree and apply common bounds in the API and verifier."""
    ordinary(root, directory=True)
    size = 0
    listing = {}
    aliases = set()
    for base, folders, files in os.walk(root, onerror=reject_walk_error):
        folders.sort()
        for name in sorted(folders + files):
            target = Path(base) / name
            relative = target.relative_to(root).as_posix()
            parts = safe_parts(relative)
            key = tuple(unicodedata.normalize("NFC", part).casefold() for part in parts)
            if key in aliases or len(aliases) >= MAX_FILES * 2:
                raise ValueError("Repository has colliding paths or too many entries")
            aliases.add(key)
            info = ordinary(target, directory=name in folders)
            if omit_git and name.casefold() == ".git":
                if name in folders:
                    folders.remove(name)
                continue
            if omit_bytecode and (name.casefold() == "__pycache__" or target.suffix.casefold() in {".pyc", ".pyo"}):
                if name in folders:
                    folders.remove(name)
                continue
            if directories is not None and stat.S_ISDIR(info.st_mode):
                directories.append(relative)
            if stat.S_ISREG(info.st_mode):
                size += info.st_size
                if info.st_size > MAX_FILE or size > MAX_BYTES or len(listing) >= MAX_FILES:
                    raise ValueError("Repository exceeds the package size limit")
                listing[relative] = target
    return listing


def resolve(root, relative):
    parts = safe_parts(relative)
    target = root
    ordinary(root, directory=True)
    for index, part in enumerate(parts):
        target /= part
        try:
            ordinary(target, directory=index < len(parts) - 1)
        except FileNotFoundError:
            continue
    return target


def checked_input(path, expected):
    ordinary(path)
    if digest(path) != expected:
        raise ValueError("Packaged input checksum mismatch")
    return json.loads(path.read_bytes())


def verify_source(manifest_path, manifest_sha):
    manifest = checked_input(manifest_path, manifest_sha)
    directories = []
    files = tree(ROOT, directories=directories)
    if (manifest["files"] != {path: digest(file) for path, file in files.items()}
            or manifest["directories"] != sorted(directories)):
        raise ValueError("The starting tree does not match the verified snapshot manifest.")
    print("Verified starting snapshot " + manifest["snapshot_id"])


def apply_oracle(directory, expected):
    plan = checked_input(directory / "changes.json", expected)
    pending = []
    for change in plan:
        target = resolve(ROOT, change["path"])
        before = digest(target) if target.exists() else None
        after = change["after"]
        payload = None
        if after is not None:
            payload = resolve(directory / "files", change["payload"]).read_bytes()
            if hashlib.sha256(payload).hexdigest() != after:
                raise ValueError("Oracle replacement checksum mismatch")
        if before == after:
            continue
        if before != change["before"]:
            raise ValueError("Oracle starting-file checksum mismatch")
        pending.append((target, payload))
    for target, payload in pending:
        if payload is None:
            target.unlink()
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            permissions = target.stat().st_mode & 0o777 if target.exists() else 0o644
            with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as staged:
                temporary = Path(staged.name)
                staged.write(payload)
            try:
                temporary.chmod(permissions)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
    for change in plan:
        target = resolve(ROOT, change["path"])
        if (digest(target) if target.exists() else None) != change["after"]:
            raise ValueError("Oracle change did not persist")
    print(f"Verified {len(plan)} oracle file changes")


def run_native(repository, plan, result_path):
    import resource

    def limit_output():
        resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
        resource.setrlimit(resource.RLIMIT_NPROC, (64, 64))
        resource.setrlimit(resource.RLIMIT_NOFILE, (256, 256))

    with tempfile.TemporaryFile() as output:
        completed = subprocess.run(
            [PYTHON, "-I", "-B", __file__, "native", str(repository), json.dumps(plan)],
            cwd="/", env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/tmp",
                          "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1", "LANG": "C.UTF-8"},
            stdout=output, stderr=subprocess.STDOUT, timeout=45, check=False,
            user=65534, group=65534, extra_groups=(), preexec_fn=limit_output,
        )
        output.seek(0)
        raw = output.read(MAX_OUTPUT)
    result_path.write_bytes(raw)
    outcome = json.loads(raw)
    cases = outcome["cases"]
    actual = {(case["path"], case["selector"]) for case in cases}
    planned = {(selector["path"], selector["selector"]) for selector in plan["selectors"]}
    evidence_ok = completed.returncode == outcome["exit_code"] == 0 and not outcome["failure_category"]
    passed = evidence_ok and (
        not cases if plan["kind"] == "compile" else bool(cases)
        and all(case["outcome"] == "passed" for case in cases)
        and {case["path"] for case in cases} == set(plan["targets"])
        and (not planned or actual == planned)
    )
    return {"group": plan["group"], "exit_code": completed.returncode, "passed": passed, "cases": len(cases)}


def native(repository, plan):
    sys.path.insert(0, str(Path(__file__).parent))
    import sandbox_guest

    sandbox_guest.BUNDLE = Path("/tests")
    sandbox_guest.REPO = repository
    sandbox_guest.SITE = "/opt/ctf/site"
    if plan["kind"] == "compile":
        with tempfile.TemporaryDirectory(prefix="ctf-bytecode-") as temporary:
            sandbox_guest.BYTECODE = temporary
            return sandbox_guest.emit_result(plan)
    return sandbox_guest.emit_result(plan)


def verified_regression_inputs(directory, manifest, submission):
    """Use pristine test bytes even after Harbor's documented Windows upload transform."""
    exact_files = {}
    for path, expected in manifest["original_inputs"].items():
        if path not in submission:
            raise ValueError("A regression test or fixture is missing")
        actual = digest(submission[path])
        if actual == expected:
            continue
        if actual != manifest["line_ending_recovery"].get(path):
            raise ValueError("Original regression tests or fixtures changed")
        exact = resolve(directory / "private" / "original", path)
        if digest(exact) != expected:
            raise ValueError("Packaged regression test checksum mismatch")
        exact_files[path] = exact
    return exact_files


def verify(directory, expected):
    LOGS.mkdir(parents=True, exist_ok=True)
    LOGS.chmod(0o700)
    reward = LOGS / "reward.txt"
    reward.write_text("0\n", encoding="utf-8")
    outcomes = []
    status = "invalid_evidence"
    try:
        if {name for _, name in socket.if_nameindex()} - {"lo"}:
            raise ValueError("The verifier requires a network-disabled Docker container")
        manifest = checked_input(directory / "plan.json", expected)
        directories = []
        submission = tree(ROOT, omit_bytecode=True, directories=directories)
        try:
            submission.update(verified_regression_inputs(directory, manifest, submission))
        except ValueError:
            status = "regression_protection"
            raise
        original_paths = set(manifest["original_paths"])
        if any(Path(path).name.casefold() in {"conftest.py", ".pytest.ini", "pytest.ini", "pytest.toml", ".pytest.toml"}
               for path in submission.keys() - original_paths):
            status = "regression_protection"
            raise ValueError("Submission adds a test runner control file")
        with tempfile.TemporaryDirectory(prefix="ctf-harbor-") as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            root.parent.chmod(0o755)
            for directory_name in directories:
                root.joinpath(*safe_parts(directory_name)).mkdir(parents=True, exist_ok=True)
            for path, file in submission.items():
                target = root.joinpath(*safe_parts(path))
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(file, target)
            for plan in manifest["groups"]:
                staged = []
                for path, expected_sha in plan["files"].items():
                    private = resolve(directory / "private" / plan["group"], path)
                    if digest(private) != expected_sha:
                        raise ValueError("Private test checksum mismatch")
                    target = resolve(root, path)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("xb") as output:
                        output.write(private.read_bytes())
                    staged.append(target)
                outcome = run_native(root, plan, LOGS / (plan["group"] + ".json"))
                outcomes.append(outcome)
                for target in staged:
                    target.unlink()
                if plan["kind"] == "compile" and not outcome["passed"]:
                    status = "build_failed"
                    return 1
        if not all(item["passed"] for item in outcomes):
            status = "tests_failed"
            return 1
        reward.write_text("1\n", encoding="utf-8")
        status = "passed"
        return 0
    except subprocess.TimeoutExpired:
        status = "timeout"
        print("A native command exceeded its deadline.", file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
        print(f"Verifier stopped: {exc}", file=sys.stderr)
        return 1
    finally:
        (LOGS / "execution.json").write_text(json.dumps(outcomes), encoding="utf-8")
        (LOGS / "status.json").write_text(json.dumps({"status": status}), encoding="utf-8")


if __name__ == "__main__":
    operation, path, argument = sys.argv[1:]
    if operation == "source":
        verify_source(Path(path), argument)
    elif operation == "oracle":
        apply_oracle(Path(path), argument)
    elif operation == "verify":
        raise SystemExit(verify(Path(path), argument))
    elif operation == "native":
        raise SystemExit(native(Path(path), json.loads(argument)))
    else:
        raise SystemExit(2)
