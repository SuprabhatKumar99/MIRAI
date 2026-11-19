"""Local-only defaults, optional tenant credentials, and bounded request admission."""

from contextvars import ContextVar
from hashlib import sha256
from hmac import compare_digest
from ipaddress import ip_address
import json
import os
import re
from tempfile import SpooledTemporaryFile
from urllib.parse import urlsplit

from starlette.responses import JSONResponse
from starlette.concurrency import run_in_threadpool
from sqlite3 import Error as SQLiteError

from app.services import workflow_store as store

OWNER = ContextVar("ctf_principal", default="local")
COOKIE = "ctf_access"
MODEL_PATHS = {
    "/tasks/generate", "/tasks/generate/from-repo", "/tasks/generate/github", "/tasks/generate/upload",
    "/tasks/solution", "/tasks/tests",
}


def principal():
    return OWNER.get()


def shared():
    return os.getenv("CTF_SHARED_MODE") == "1"


def origins():
    configured = os.getenv("CTF_ALLOWED_ORIGINS")
    values = (configured.split(",") if configured else
              ([] if shared() else ["http://localhost:5173", "http://127.0.0.1:5173", "http://localhost:3000"]))
    result = []
    for value in values:
        value = value.strip().rstrip("/")
        parsed = urlsplit(value)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.path or parsed.query
                or parsed.fragment or parsed.username or parsed.password or "*" in value):
            raise RuntimeError("CTF_ALLOWED_ORIGINS must contain explicit HTTP origins.")
        if shared() and parsed.scheme != "https":
            raise RuntimeError("Shared deployment origins require HTTPS.")
        result.append(value)
    return result


def _tokens():
    data = json.loads(os.getenv("CTF_ACCESS_TOKENS", "{}"))
    if not isinstance(data, dict) or not 1 <= len(data) <= 256 or any(
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", owner)
        or owner in {"local", "global"}
        or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token)
        for owner, token in data.items()
    ) or len(set(data.values())) != len(data):
        raise ValueError("Shared mode requires unique operator access tokens.")
    return {owner: sha256(token.encode()).digest() for owner, token in data.items()}


def _enabled_limit(name, default):
    value = int(os.getenv(name, str(default)))
    if value < 0:
        raise ValueError("Quota limits must be nonnegative.")
    return value


def reserve_model(owner, amount):
    return store.reserve(owner, store.now()[:10] + ":model", amount,
                         _enabled_limit("CTF_MODEL_REQUESTS_PER_DAY", 64),
                         global_limit=_enabled_limit("CTF_GLOBAL_MODEL_REQUESTS_PER_DAY", 256))


def _local(scope):
    client = scope.get("client")
    if not client:
        return False
    # Starlette's in-process TestClient uses this symbolic peer, unavailable to real socket clients.
    if client[0] == "testclient":
        return True
    try:
        return ip_address(client[0]).is_loopback
    except ValueError:
        return False


async def _respond(scope, receive, send, status, detail, **headers):
    await JSONResponse({"detail": detail}, status_code=status, headers=headers)(scope, receive, send)


class AccessMiddleware:
    def __init__(self, app, allowed_origins):
        self.app = app
        self.allowed_origins = set(allowed_origins)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] == "OPTIONS":
            await self.app(scope, receive, send)
            return
        path, method = scope["path"], scope["method"]
        headers = {name.decode("latin1"): value.decode("latin1") for name, value in scope["headers"]}
        if path == "/health" or path == "/auth/session" and method == "GET":
            await self.app(scope, receive, send)
            return
        owner = "local"
        if shared():
            if path in {"/docs", "/redoc", "/openapi.json"}:
                await _respond(scope, receive, send, 404, "Not found.")
                return
            try:
                tokens = _tokens()
            except (TypeError, ValueError):
                await _respond(scope, receive, send, 503, "Shared access is unavailable.")
                return
            authorization = headers.get("authorization", "")
            scheme, _, bearer = authorization.partition(" ")
            if authorization and (scheme.lower() != "bearer" or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", bearer)):
                await _respond(scope, receive, send, 401, "Sign in with an operator access token.")
                return
            cookie = next((value.strip().partition("=")[2] for value in headers.get("cookie", "").split(";")
                           if value.strip().partition("=")[0] == COOKIE), "")
            token = bearer or cookie
            if path == "/auth/session" and method == "POST" and not bearer:
                token = ""
            secret = sha256(token[:128].encode()).digest() if len(token) <= 128 else bytes(32)
            owner = next((key for key, value in tokens.items() if compare_digest(value, secret)), None)
            if not owner:
                try:
                    peer = (scope.get("client") or ("unknown",))[0]
                    fingerprint = sha256(peer.encode()).hexdigest()[:24]
                    admitted = await run_in_threadpool(
                        store.reserve, "auth-" + fingerprint, store.now()[:16] + ":auth", 1, 30,
                    )
                    if not admitted:
                        await _respond(scope, receive, send, 429, "Sign-in quota reached.", **{"Retry-After": "60"})
                        return
                except (OSError, ValueError, SQLiteError):
                    await _respond(scope, receive, send, 503, "Shared access is unavailable.")
                    return
                await _respond(scope, receive, send, 401, "Sign in with an operator access token.")
                return
            if method not in {"GET", "HEAD"} and cookie and not bearer and headers.get("origin") not in self.allowed_origins:
                await _respond(scope, receive, send, 403, "This origin cannot modify workflow records.")
                return
        elif not _local(scope):
            await _respond(scope, receive, send, 403, "This API is available to the local operator.")
            return
        if method not in {"GET", "HEAD"} and headers.get("origin") and headers["origin"] not in self.allowed_origins:
            await _respond(scope, receive, send, 403, "This origin cannot modify workflow records.")
            return

        try:
            if not await run_in_threadpool(store.reserve, owner, store.now()[:16] + ":http", 1,
                                           _enabled_limit("CTF_REQUESTS_PER_MINUTE", 240)):
                await _respond(scope, receive, send, 429, "Request quota reached.", **{"Retry-After": "60"})
                return
        except (OSError, ValueError, SQLiteError):
            await _respond(scope, receive, send, 503, "The workflow store is unavailable.")
            return

        token = OWNER.set(owner)
        try:
            if method not in {"POST", "PUT", "PATCH"}:
                await self.app(scope, receive, send)
                return
            if headers.get("content-encoding", "identity").lower() != "identity":
                await _respond(scope, receive, send, 415, "Compressed requests are unsupported.")
                return
            limit = 25 * 1024 * 1024 + 128 * 1024 if path in {
                "/tasks/analyze/upload", "/tasks/generate/upload"
            } else 4 * 1024 * 1024
            try:
                stated = int(headers.get("content-length", 0))
            except ValueError:
                await _respond(scope, receive, send, 400, "Invalid request length.")
                return
            if stated > limit or stated < 0:
                await _respond(scope, receive, send, 413, "Request exceeds the size limit.")
                return
            with SpooledTemporaryFile(max_size=1024 * 1024) as stream:
                size = 0
                while True:
                    block = await receive()
                    if block["type"] == "http.disconnect":
                        return
                    chunk = block.get("body", b"")
                    size += len(chunk)
                    if size > limit:
                        await _respond(scope, receive, send, 413, "Request exceeds the size limit.")
                        return
                    stream.write(chunk)
                    if not block.get("more_body", False):
                        break
                if path.rstrip("/") in MODEL_PATHS:
                    try:
                        allowed = await run_in_threadpool(reserve_model, owner, 2)
                    except (OSError, ValueError, SQLiteError):
                        await _respond(scope, receive, send, 503, "Model request admission is unavailable.")
                        return
                    if not allowed:
                        await _respond(scope, receive, send, 429, "Daily model request budget reached.")
                        return
                stream.seek(0)
                sent = False

                async def replay():
                    nonlocal sent
                    if sent:
                        return await receive()
                    data = stream.read(65536)
                    sent = stream.tell() == size
                    return {"type": "http.request", "body": data, "more_body": not sent}

                await self.app(scope, replay, send)
        finally:
            OWNER.reset(token)
