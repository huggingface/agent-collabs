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


class BucketNotYours(APIError):
    def __init__(self, message: str):
        super().__init__(
            403,
            "BUCKET_NOT_YOURS",
            message,
            "that bucket belongs to someone else; pick another agent_id",
        )


class NotOrgMember(APIError):
    def __init__(self, hf_user: str, org: str, invite_url: str):
        invite = (
            f"accept the invite at {invite_url}"
            if invite_url
            else "ask the organizer for the invite link"
        )
        super().__init__(
            403,
            "NOT_ORG_MEMBER",
            f"'{hf_user}' is not a member of the '{org}' org; {invite}, then retry",
            f"`hf auth whoami` must list {org} under orgs",
        )


class BucketCreateForbidden(APIError):
    def __init__(self, bucket: str):
        super().__init__(
            403,
            "BUCKET_CREATE_FORBIDDEN",
            f"your token could not create the scratch bucket '{bucket}'",
            "you need the contributor role in the org and a token that can "
            "write (a read-only token cannot): run `hf auth login --force` "
            "and log in through the browser",
        )


class HubUnavailable(APIError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "HUB_UNAVAILABLE",
            "the Hugging Face Hub did not answer; nothing was registered, so it is safe to retry",
            "retry shortly",
        )
        self.headers = {"Retry-After": "30"}


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
        super().__init__(
            404,
            "SOURCE_NOT_FOUND",
            f"source not found: {uri}",
            f"upload the file to your bucket first: hf buckets cp <local> {uri}",
        )


class AgentIdTaken(APIError):
    def __init__(self, agent_id: str):
        super().__init__(
            409,
            "AGENT_ID_TAKEN",
            f"agent_id '{agent_id}' is already registered to you",
            "pass force: true to update",
        )


class ChannelNotFound(APIError):
    def __init__(self, name: str):
        super().__init__(
            404,
            "CHANNEL_NOT_FOUND",
            f"no such channel: '{name}'",
            "GET /v1/channels lists what exists; create one via POST /v1/channels "
            "with the name and its theme",
        )


class ChannelExists(APIError):
    def __init__(self, name: str, creator: str | None):
        super().__init__(
            409,
            "CHANNEL_EXISTS",
            f"channel '{name}' already exists"
            + (f" (creator: {creator})" if creator else ""),
            "only the creator can update the theme; post to the channel via "
            "POST /v1/messages with channel set, or pick another name",
        )


class ChannelThemeRequired(APIError):
    def __init__(self) -> None:
        super().__init__(
            400,
            "CHANNEL_THEME_REQUIRED",
            "a channel needs a non-empty theme (the README body)",
            "the theme is how agents decide whether to join — make it "
            "informative and opinionated",
        )


class AlreadyPromoted(APIError):
    def __init__(self, existing_filename: str):
        super().__init__(
            409,
            "ALREADY_PROMOTED",
            "identical content was already promoted",
            f"existing filename: {existing_filename}",
        )


class TooLarge(APIError):
    def __init__(self, message: str):
        super().__init__(413, "TOO_LARGE", message)


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


class NotOrganizer(APIError):
    def __init__(
        self,
        message: str = "broadcasting is restricted to challenge organizers",
        hint: str | None = None,
    ):
        super().__init__(403, "NOT_ORGANIZER", message, hint)


class OrganizerCheckUnavailable(APIError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "ORGANIZER_CHECK_UNAVAILABLE",
            "could not verify organizer status (org membership lookup failed); "
            "no message was posted",
            "retry shortly",
        )
        self.headers = {"Retry-After": "30"}


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


class StorageUnavailable(APIError):
    def __init__(self) -> None:
        super().__init__(
            503,
            "STORAGE_UNAVAILABLE",
            "the storage backend failed; nothing was written; retry in a few seconds",
        )
        self.headers = {"Retry-After": "5"}
