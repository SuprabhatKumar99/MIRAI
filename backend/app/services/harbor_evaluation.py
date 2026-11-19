"""Pinned Harbor CLI execution and evidence collection."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
from threading import Thread

from app.schemas.evaluation import (
    AGENT_NAME, AGENT_VERSION, ChangedFile, ProcessEvidence, TestCounts, TrialEvidence,
)
from app.services.harbor_guest import digest, ordinary, tree

HARBOR_VERSION = "0.25.0"
AGENT_IMPORT = "harbor_ctf_agent:CTFFileAgent"
AGENT_SOURCE = Path(__file__).with_name("harbor_ctf_agent.py")
PROVIDER_KEY = "CTF_EVAL_PROVIDER_KEY"
MAX_CONSOLE = 1024 * 1024
TRIAL_TIMEOUT = 480


class RunnerBlocked(RuntimeError):
    def __init__(self, category, message):
        super().__init__(message)
        self.category = category


def utcnow():
    return datetime.now(timezone.utc)


def _credential_name(model):
    return {"openai": "OPENAI_API_KEY", "huggingface": "HF_TOKEN", "gemini": "GEMINI_API_KEY"}.get(
        model.split("/", 1)[0], "OPENAI_API_KEY",
    )


def _controller_environment(*, provider=False, model="openai/default"):
    names = {"SYSTEMROOT", "WINDIR", "PATH", "PATHEXT", "TEMP", "TMP", "TMPDIR", "USERPROFILE",
             "LOCALAPPDATA", "APPDATA", "HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "PROGRAMFILES",
             "PROGRAMFILES(X86)", "PROGRAMDATA", "SYSTEMDRIVE", "COMSPEC"}
    env = {key: value for key, value in os.environ.items() if key.upper() in names}
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parent), PYTHONUTF8="1",
        DO_NOT_TRACK="1", LITELLM_LOCAL_MODEL_COST_MAP="True", NO_COLOR="1",
        DOCKER_HOST="npipe:////./pipe/dockerDesktopLinuxEngine" if os.name == "nt" else "unix:///var/run/docker.sock",
    )
    credential = _credential_name(model)
    if provider and (key := os.getenv(credential, "").strip()):
        env[PROVIDER_KEY] = key
    return env


def _terminate(process):
    if os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        system_root = Path(os.getenv("SystemRoot", "C:/Windows"))
        try:
            subprocess.run([str(system_root / "System32/taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()


def _command(argv, cwd, timeout, env):
    started = utcnow()
    with subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, start_new_session=os.name != "nt", bufsize=0,
    ) as process:
        output = bytearray()
        truncated = False

        def read():
            nonlocal truncated
            while chunk := process.stdout.read(65536):
                available = MAX_CONSOLE - len(output)
                output.extend(chunk[:available])
                truncated |= len(chunk) > available

        reader = Thread(target=read, daemon=True)
        reader.start()
        expired = False
        try:
            process.wait(timeout)
        except subprocess.TimeoutExpired:
            expired = True
            _terminate(process)
            process.wait(12)
        reader.join(5)
        if reader.is_alive():
            truncated = True
        return process.returncode, bytes(output), expired, truncated, started, utcnow()


def _subpath(root, relative):
    path = root
    parts = Path(relative).parts
    for index, part in enumerate(parts):
        path /= part
        ordinary(path, directory=index < len(parts) - 1)
    return path


def _bytes(root, relative, limit=4 * 1024 * 1024):
    path = _subpath(root, relative)
    with path.open("rb") as reader:
        data = reader.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Oversized Harbor receipt.")
    return data


def _json(root, relative):
    return json.loads(_bytes(root, relative))


def _rel(root, path):
    return path.relative_to(root).as_posix()


def _trial_directory(job):
    candidates = [
        path.parent for path in (*job.glob("*/result.json"), *job.glob("trials/*/result.json"))
        if path.parent != job
    ]
    if len(candidates) != 1:
        raise ValueError("Harbor did not emit exactly one trial.")
    current = candidates[0]
    while current != job.parent:
        ordinary(current, directory=True)
        current = current.parent
    return candidates[0]


def _duration(raw):
    duration = raw.get("agent_execution") or {}
    try:
        return max(0, (datetime.fromisoformat(duration["finished_at"]) -
                       datetime.fromisoformat(duration["started_at"])).total_seconds())
    except (KeyError, TypeError, ValueError):
        return None


def _classify_exception(name):
    name = name.lower()
    if "timeout" in name:
        return "timeout"
    if any(word in name for word in ("authentication", "provider", "ratelimit", "credential", "apikey")):
        return "provider_unavailable"
    if "agent" in name:
        return "agent_crash"
    return "environment_failure"


def _changed_files(submission, originals):
    current = {relative: digest(path) for relative, path in tree(submission, omit_bytecode=True).items()}
    output = []
    for relative in sorted(current.keys() | originals.keys()):
        before, after = originals.get(relative), current.get(relative)
        if before == after:
            continue
        operation = "create" if before is None else "delete" if after is None else "modify"
        output.append(ChangedFile(path=relative, operation=operation, before_sha256=before, after_sha256=after))
    return output


def _score_groups(trial, plan, rows):
    expected = {group["group"]: group for group in plan["groups"]}
    if not isinstance(rows, list) or len(rows) > len(expected):
        raise ValueError("Invalid verifier groups.")
    seen = {}
    counts = {}
    for row in rows:
        group = row["group"]
        if group not in expected or group in seen or type(row["passed"]) is not bool:
            raise ValueError("Unexpected or duplicate verifier group.")
        receipt = _json(trial, f"verifier/{group}.json")
        cases = receipt["cases"]
        if (type(receipt["exit_code"]) is not int or type(row["exit_code"]) is not int
                or receipt["exit_code"] != row["exit_code"] or not isinstance(cases, list)
                or type(row["cases"]) is not int or len(cases) != row["cases"] or len(cases) > 512):
            raise ValueError("Native receipt does not match verifier summary.")
        allowed_outcomes = {"passed", "assertion_failed", "error", "skipped"}
        if any(case["outcome"] not in allowed_outcomes for case in cases):
            raise ValueError("Unknown test receipt outcome.")
        total_passed = sum(case["outcome"] == "passed" for case in cases)
        if group == "build":
            consistent = not cases
            passed = receipt["exit_code"] == 0 and receipt["failure_category"] is None
        else:
            targets = set(expected[group]["targets"])
            selectors = {(item["path"], item["selector"]) for item in expected[group]["selectors"]}
            observed = {(case["path"], case["selector"]) for case in cases}
            consistent = {case["path"] for case in cases} <= targets
            passed = (receipt["exit_code"] == 0 and receipt["failure_category"] is None
                      and bool(cases) and total_passed == len(cases)
                      and {case["path"] for case in cases} == targets
                      and (not selectors or observed == selectors))
            if cases:
                counts[group] = TestCounts(passed=total_passed, total=len(cases))
        if not consistent or passed != row["passed"]:
            raise ValueError("Native receipt contradicts the verifier summary.")
        seen[group] = passed
    return expected, seen, counts


def parse_trial(root, job, evidence, plan, originals):
    """Require official trial identity and independent verifier receipts before trusting rewards."""
    trial = _trial_directory(job)
    raw = _json(trial, "result.json")
    evidence.evidence["trial_result"] = _rel(root, trial / "result.json")
    trial_id, checksum = raw["id"], raw["task_checksum"]
    if not re.fullmatch(r"[0-9a-f-]{36}", trial_id) or not re.fullmatch(r"[A-Fa-f0-9]{32,128}", checksum):
        raise ValueError("Missing canonical Harbor trial identity.")
    evidence.harbor_trial_id = trial_id
    evidence.harbor_task_checksum = checksum
    info = raw["agent_info"]
    evidence.agent_name, evidence.agent_version = info["name"], info["version"]
    expected_name = {"agent": AGENT_NAME, "oracle": "oracle", "control": "nop"}[evidence.role]
    if evidence.agent_name != expected_name or (evidence.role == "agent" and evidence.agent_version != AGENT_VERSION):
        raise ValueError("Harbor ran a different agent.")
    evidence.agent_duration_seconds = _duration(raw)
    result = raw.get("agent_result") or {}
    meta = result.get("metadata") or {}
    provider_code = meta.get("ctf_provider_error_code")
    if type(provider_code) is int and 400 <= provider_code <= 599:
        messages = {
            400: "Provider rejected the chat request or JSON response format.",
            401: "Provider token is invalid or expired.",
            402: "Provider inference credits or billing are required.",
            403: "Provider token lacks permission or access to this model.",
            404: "Provider cannot serve the requested model.",
            422: "Provider rejected the model parameters or JSON response format.",
            429: "Provider rate limit or inference quota exceeded.",
        }
        evidence.flags.append(f"Provider HTTP {provider_code}. " + messages.get(
            provider_code, "Provider service could not complete the request.",
        ))
    calls = meta.get("ctf_provider_completed_calls")
    if type(calls) is int and 0 <= calls <= 8:
        evidence.provider_calls = calls
    models = meta.get("ctf_resolved_models") or []
    evidence.resolved_models = [model for model in models[:8] if isinstance(model, str)
                                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,239}", model)]
    for field, source in (("input_tokens", "n_input_tokens"), ("cached_tokens", "n_cache_tokens"),
                          ("output_tokens", "n_output_tokens"), ("cost_usd", "cost_usd")):
        value = result.get(source)
        if type(value) in ((int, float) if field == "cost_usd" else (int,)) and 0 <= value < 10**9:
            setattr(evidence, field, value)

    if evidence.role == "agent":
        try:
            trajectory = _json(trial, "agent/trajectory.json")
            if (trajectory.get("schema_version") == "ATIF-v1.8" and trajectory.get("steps")
                    and trajectory["agent"]["name"] == AGENT_NAME):
                evidence.evidence["trajectory"] = _rel(root, trial / "agent/trajectory.json")
        except (FileNotFoundError, KeyError, TypeError, ValueError):
            pass

    if exception := raw.get("exception_info"):
        name = exception.get("exception_type", "unknown")
        evidence.exception_type = str(name)[:100]
        evidence.failure = _classify_exception(evidence.exception_type)
        return
    if raw.get("verifier_environment_mode") != "separate" or not (raw.get("verifier") or {}).get("finished_at"):
        raise ValueError("Missing independent verifier execution.")
    if evidence.role == "agent":
        model_info = info.get("model_info") or {}
        requested = evidence.configuration.model
        provider, model_name = requested.split("/", 1)
        if model_info.get("provider") != provider or model_info.get("name") != model_name:
            raise ValueError("Harbor used a different model.")
        if not evidence.provider_calls or not evidence.resolved_models or "trajectory" not in evidence.evidence:
            raise ValueError("Missing real provider execution evidence.")
        if evidence.provider_calls > evidence.configuration.max_turns:
            raise ValueError("Agent exceeded its recorded budget.")

    reward = (raw["verifier_result"]["rewards"])["reward"]
    raw_reward = _bytes(trial, "verifier/reward.txt", 24).decode().strip()
    if type(reward) not in (int, float) or reward not in (0, 1) or raw_reward not in ("0", "1", "0.0", "1.0"):
        raise ValueError("Missing binary verifier reward.")
    if reward != float(raw_reward):
        raise ValueError("Harbor and verifier reward disagree.")
    for name in ("execution.json", "status.json", "reward.txt"):
        evidence.evidence[name] = _rel(root, trial / "verifier" / name)
    rows = _json(trial, "verifier/execution.json")
    expected, seen, counts = _score_groups(trial, plan, rows)
    for group in seen:
        evidence.evidence[group] = _rel(root, trial / "verifier" / f"{group}.json")
    evidence.build_passed = seen.get("build")
    evidence.feature_tests = counts.get("feature")
    if "feature" in seen:
        evidence.feature_failure_kind = _json(trial, "verifier/feature.json")["failure_category"]
    regression = [counts[name] for name in ("original_regression", "regression") if name in counts]
    if regression:
        evidence.regression_tests = TestCounts(passed=sum(count.passed for count in regression),
                                              total=sum(count.total for count in regression))
    submission = trial / "artifacts/submission"
    ordinary(submission.parent, directory=True)
    evidence.submitted_files = _changed_files(submission, originals)
    evidence.evidence["submission"] = _rel(root, submission)
    status = _json(trial, "verifier/status.json")["status"]
    if status == "passed":
        valid = reward == 1 and seen.keys() == expected.keys() and all(seen.values())
    elif status == "build_failed":
        valid = reward == 0 and seen == {"build": False}
        evidence.failure = "build_failed"
    elif status == "tests_failed":
        valid = reward == 0 and seen.keys() == expected.keys() and seen.get("build") and not all(seen.values())
        if not seen.get("original_regression") or seen.get("regression") is False:
            evidence.failure = "regression_failed"
        elif evidence.submitted_files:
            evidence.failure = "feature_failed"
        else:
            evidence.failure = "agent_incomplete"
    elif status == "regression_protection":
        valid = reward == 0 and not seen
        evidence.failure = "regression_protection"
    elif status == "timeout":
        evidence.failure = "timeout"
        return
    else:
        valid = False
    if not valid:
        raise ValueError("Verifier state contradicts its reward or execution evidence.")
    evidence.reward = int(reward)
    evidence.trustworthy = True
    if evidence.role == "agent" and len(evidence.resolved_models) != 1:
        evidence.flags.append("multiple_provider_model_versions")
        evidence.trustworthy = False
        evidence.failure = "invalid_evidence"


class HarborEvaluationBackend:
    origin = "harbor"

    def probe(self, root, *, live):
        if live and not any(os.getenv(name, "").strip() for name in ("OPENAI_API_KEY", "HF_TOKEN", "GEMINI_API_KEY")):
            raise RunnerBlocked("provider_unavailable", "Configure OPENAI_API_KEY, HF_TOKEN or GEMINI_API_KEY on the evaluation controller.")
        executable = os.getenv("CTF_HARBOR_EXECUTABLE") or shutil.which("harbor")
        docker = shutil.which("docker")
        if not executable or not docker:
            raise RunnerBlocked("environment_failure", "Harbor 0.25.0 and local Docker are required.")
        self.executable = str(Path(executable).resolve())
        env = _controller_environment()
        try:
            code, out, expired, *_ = _command([self.executable, "--version"], root, 20, env)
            if code != 0 or expired or HARBOR_VERSION.encode() not in out.strip().splitlines()[-1:]:
                raise RunnerBlocked("environment_failure", "The configured Harbor CLI must be version 0.25.0.")
            code, out, expired, *_ = _command(
                [str(Path(docker).resolve()), "info", "--format", "{{.OSType}}|{{.ServerVersion}}|{{.KernelVersion}}"],
                root, 12, env,
            )
            engine = out.decode(errors="replace").strip()
            if code != 0 or expired or not engine.startswith("linux|"):
                raise RunnerBlocked("environment_failure", "The local Linux Docker engine is unavailable.")
            code, out, expired, *_ = _command([str(Path(docker).resolve()), "compose", "version"], root, 12, env)
            if code != 0 or expired or b"Docker Compose" not in out:
                raise RunnerBlocked("environment_failure", "Docker Compose is unavailable to the evaluation controller.")
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RunnerBlocked("environment_failure", "The local Harbor or Docker process could not start.") from exc
        return HARBOR_VERSION, engine

    def run(self, task_directory, root, evidence, plan, originals):
        if configuration := evidence.configuration:
            if not os.getenv(_credential_name(configuration.model), "").strip():
                if configuration.fallback_model and os.getenv(_credential_name(configuration.fallback_model), "").strip():
                    evidence.requested_model = configuration.model
                    evidence.configuration = configuration.model_copy(update={
                        "model": configuration.fallback_model, "fallback_model": None,
                    })
                    evidence.flags.append(
                        "huggingface_fallback_openai_key_missing" if configuration.fallback_model.startswith("huggingface/")
                        else "gemini_fallback_primary_key_missing"
                    )
            credential = _credential_name(evidence.configuration.model)
            if not os.getenv(credential, "").strip():
                evidence.failure = "provider_unavailable"
                return
        name = f"{evidence.role}-{evidence.label}-{evidence.attempt}"
        runner = AGENT_IMPORT if evidence.role == "agent" else "oracle" if evidence.role == "oracle" else "nop"
        argv = [self.executable, "run", "--path", str(task_directory), "--agent", runner, "--env", "docker",
                "--n-concurrent", "1", "--jobs-dir", str(root / "jobs"), "--job-name", name,
                "--agent-timeout-multiplier", "0.2", "--environment-build-timeout-multiplier", "0.25"]
        if configuration := evidence.configuration:
            argv.extend(["--model", configuration.model, "--ak", f"max_turns={configuration.max_turns}",
                         "--ak", f"max_output_tokens={configuration.max_output_tokens}"])
            if configuration.temperature is not None:
                argv.extend(["--ak", f"temperature={configuration.temperature}"])
        env = _controller_environment(provider=evidence.role == "agent",
                                      model=evidence.configuration.model if evidence.configuration else "")
        log = root / f"{name}.log"
        try:
            code, output, expired, truncated, started, finished = _command(argv, root, TRIAL_TIMEOUT, env)
        except (OSError, subprocess.TimeoutExpired):
            evidence.failure = "environment_failure"
            return
        if key := env.get(PROVIDER_KEY):
            output = output.replace(key.encode(), b"[REDACTED]")
        log.write_bytes(output)
        evidence.process = ProcessEvidence(argv=argv, exit_code=code, console_log=_rel(root, log),
                                           started_at=started, finished_at=finished, timed_out=expired,
                                           output_truncated=truncated)
        if expired:
            evidence.failure = "timeout"
            return
        try:
            parse_trial(root, root / "jobs" / name, evidence, plan, originals)
        except (OSError, KeyError, IndexError, TypeError, ValueError, RecursionError):
            evidence.failure = "invalid_evidence" if code == 0 else "environment_failure"
            return
        if code != 0:
            evidence.trustworthy = False
            evidence.reward = None
            evidence.failure = evidence.failure or "environment_failure"
