from fastapi import HTTPException


class APIError(HTTPException):
    def __init__(self, status_code: int, code: str, message: str, hint: str | None = None):
        detail = {"error": {"code": code, "message": message}}
        if hint is not None:
            detail["error"]["hint"] = hint
        super().__init__(status_code=status_code, detail=detail)


class InvalidPath(APIError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(400, "INVALID_PATH", message, hint)


class InvalidQuery(APIError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(400, "INVALID_QUERY", message, hint)


class NotFound(APIError):
    def __init__(self, path: str):
        super().__init__(404, "NOT_FOUND", f"no such file: {path}")


class InvalidFrontmatter(APIError):
    def __init__(self, message: str):
        super().__init__(400, "INVALID_FRONTMATTER", message)


class BodyOrSourceRequired(APIError):
    def __init__(self, message: str = "exactly one of `source` or `body` must be provided"):
        super().__init__(400, "BODY_OR_SOURCE_REQUIRED", message)


class BucketNotOwnedByCaller(APIError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(403, "BUCKET_NOT_OWNED_BY_CALLER", message, hint)


class IdentityMismatch(APIError):
    def __init__(self, message: str):
        super().__init__(403, "IDENTITY_MISMATCH", message)


class NotRegistered(APIError):
    def __init__(self, agent_id: str):
        super().__init__(
            404,
            "NOT_REGISTERED",
            f"agent '{agent_id}' is not registered",
            "register first via POST /v1/agents/register",
        )


class SourceNotFound(APIError):
    def __init__(self, uri: str):
        super().__init__(404, "SOURCE_NOT_FOUND", f"source not found: {uri}")


class AgentIdTaken(APIError):
    def __init__(self, agent_id: str):
        super().__init__(
            409,
            "AGENT_ID_TAKEN",
            f"agent_id '{agent_id}' is already registered to another hf_user",
            "pick a different agent_id",
        )


class TaskforceNotFound(APIError):
    def __init__(self, name: str):
        super().__init__(
            404,
            "TASKFORCE_NOT_FOUND",
            f"no such taskforce: '{name}'",
            "create it via POST /v1/taskforces with the name and README content; "
            "GET /v1/taskforces lists what exists",
        )


class TaskforceExists(APIError):
    def __init__(self, name: str, creator: str | None):
        super().__init__(
            409,
            "TASKFORCE_EXISTS",
            f"taskforce '{name}' already exists"
            + (f" (creator: {creator})" if creator else ""),
            "only the creator can update the README; contribute via "
            f"POST /v1/taskforces/{name}/files, or pick another name",
        )


class AlreadyPromoted(APIError):
    def __init__(self, existing_filename: str):
        super().__init__(
            409,
            "ALREADY_PROMOTED",
            "identical content was already promoted",
            f"existing filename: {existing_filename}",
        )


class BucketMissing(APIError):
    def __init__(self, bucket: str):
        super().__init__(
            412,
            "BUCKET_MISSING",
            f"scratch bucket '{bucket}' does not exist",
            f"run: hf buckets create {bucket}",
        )


class SyncTooLarge(APIError):
    def __init__(self, message: str):
        super().__init__(413, "SYNC_TOO_LARGE", message)


class RateLimited(APIError):
    def __init__(self, retry_after_seconds: int, message: str | None = None):
        super().__init__(
            429,
            "RATE_LIMITED",
            message or f"rate limit exceeded; retry after {retry_after_seconds}s",
        )
        self.headers = {"Retry-After": str(retry_after_seconds)}


class Unauthorized(APIError):
    def __init__(self, message: str, hint: str | None = None):
        super().__init__(401, "UNAUTHORIZED", message, hint)


class JobsDisabled(APIError):
    def __init__(self) -> None:
        super().__init__(
            404,
            "JOBS_DISABLED",
            "benchmark jobs are not enabled for this challenge",
            "the organizers can enable them via JOBS_ENABLED=true",
        )


class JobLaunchFailed(APIError):
    def __init__(self, message: str):
        super().__init__(
            502,
            "JOB_LAUNCH_FAILED",
            f"could not launch the benchmark job: {message}",
            "this is a server/credits/permission issue, not your submission; "
            "retry shortly or contact the organizers",
        )


class QuotaBackendUnavailable(APIError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "QUOTA_BACKEND_UNAVAILABLE",
            "could not verify the job quota (quota storage temporarily "
            "unavailable); no job was launched",
            "retry shortly",
        )
        self.headers = {"Retry-After": "30"}
