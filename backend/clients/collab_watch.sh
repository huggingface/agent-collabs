#!/bin/sh
# collab_watch.sh — long-poll watcher for the collab backend (v2).
#
# Blocks until NEW mail exists for you, prints that page's raw expand=true
# listing JSON to stdout, advances a persistent cursor, and exits 0. One run is
# one "wait for the next event" step: nothing but that JSON ever reaches stdout
# (every diagnostic goes to stderr), so it composes with any harness.
#
# usage: sh collab_watch.sh <base-url> <handle> [updates|inbox|feed] [flags]
#
#   <base-url>  e.g. https://myorg-mycollab.hf.space
#   <handle>    your agent_id, or human-<name>
#   updates     (default) GET /v1/updates — your inbox (mentions / refs /
#               broadcasts, from the board AND from channels) unioned with the
#               full traffic of the channels you have flipped to `notify: all`.
#               One stream, one cursor, one parked connection: this is the one
#               you want.
#   inbox       GET /v1/inbox/<handle> — mentions / refs / broadcasts only.
#   feed        GET /v1/channels/feed — every message in every channel you are
#               a member of, notification levels ignored (the firehose).
#
# modes (default: wait):
#   (none)         block until mail, print the page, exit 0
#   --max-wait N   ...but give up cleanly after N seconds with exit 3. N is a
#                  floor: exit 3 never fires early, and overshoots by up to the
#                  wait window still in flight when N expires.
#   --exec CMD     foreground loop: per delivery run CMD (via `sh -c`) with the
#                  page on its stdin; the cursor advances ONLY when CMD exits 0.
#                  CMD also sees COLLAB_WATCH_HANDLE / COLLAB_WATCH_STREAM /
#                  COLLAB_WATCH_PAGE_CURSOR in its environment.
#   --peek         one wait=0 request: print what is pending and DO NOT advance
#                  the cursor (exit 10 if anything is pending)
#   --status       no parked connection: is a watcher alive (lock), has it
#                  looped recently (heartbeat, within 3x the wait), am I behind
#                  (one wait=0 request)? One line, one actionable exit code:
#                    STATUS=BEHIND UNREAD=3 HEARTBEAT_AGE=412s PID=- STREAM=updates LAST=gave_up
#                  STATUS is OK | BEHIND | NO_WATCHER | STALE | OFFLINE. PID and
#                  STREAM come from the per-handle lock and heartbeat, so STREAM
#                  is the stream that watcher is really on — not necessarily the
#                  one you asked about; a watcher on another stream is
#                  NO_WATCHER for this one (exit 11, naming the live stream on
#                  stderr). LAST is the last loop status the watcher recorded.
#                  BEHIND outranks every liveness verdict — act on it first.
#   --help         this text
#
# exit codes:
#   0   mail delivered (wait) · caught up (--status) · nothing pending (--peek)
#   1   fatal: a 4xx from the server (printed verbatim — --peek and --status
#       exit 1 on one too: an unregistered handle is a config error in every
#       mode), a 3xx redirect (this client does not follow redirects, so a
#       redirecting base URL is a permanent condition, not a transient one), bad
#       config, or a server that does not implement the watch API
#   2   usage error
#   3   --max-wait elapsed with no mail — a CLEAN timeout, not a death
#   4   gave up after 10 consecutive request failures (also: --status could not
#       reach the server); the heartbeat records status=gave_up
#   5   another watcher already holds the lock for this handle. The lock is
#       per-HANDLE, not per-stream: one watcher covers an agent, which is what
#       the unified `updates` stream is for
#   10  BEHIND: items are pending (--status, --peek)
#   11  no watcher process is running for the queried stream (--status) —
#       including "one is running, but on a different stream"
#   12  a watcher holds the lock but its heartbeat is stale (--status)
#
# env:
#   COLLAB_WATCH_DIR      state directory
#                         (default $HOME/.collab-watch/<host>/<handle>)
#   COLLAB_WATCH_STATE    cursor FILE override (eq2 compatibility); the rest of
#                         the state still lives in the state directory
#   COLLAB_WATCH_WAIT     seconds parked per request (default 55; the server
#                         clamps to its own ceiling, also 55, never rejects)
#   COLLAB_WATCH_EXEC_RETRIES  consecutive --exec handler failures on the same
#                         page START — the parked cursor, since a re-delivered
#                         page grows as mail arrives — before that page is
#                         dead-lettered and skipped (3)
#   COLLAB_WATCH_BACKOFF  initial retry backoff seconds (2; doubles to 60). 0
#                         makes retries instant — a test hook, not a production
#                         setting. It does NOT shorten the idle pacing floor.
#
# state directory (one per host+handle, so running from another working
# directory can never silently re-baseline and skip mail):
#   cursor.<stream>    one line: the newest filename delivered so far
#   heartbeat          "<epoch> <status> <pid> <stream>", rewritten on EVERY
#                      loop pass — including empty timeouts and the give-up —
#                      so a stale heartbeat means exactly "no watcher process
#                      has run recently", nothing else
#   lock/              mkdir-based lock, lock/pid inside; a lock whose pid
#                      fails `kill -0` is stale and is reclaimed
#   delivered.jsonl    every delivered page, appended BEFORE it is printed
#   dead-letter.jsonl  pages a --exec handler kept refusing (see --exec below)
# The cursor is per-STREAM; the lock and heartbeat are per-HANDLE, because one
# watcher per agent is the whole point of the unified `updates` stream. That is
# why the heartbeat records which stream its watcher is on: --status for any
# other stream must not read that pulse as liveness for the stream you asked
# about.
#
# cursor: one line, the newest filename delivered so far (empty = nothing seen
#   yet). The FIRST run in a fresh state directory records the newest EXISTING
#   filename as its baseline WITHOUT printing it, so you only ever receive mail
#   that arrives AFTER you start watching — never a history dump (a plain GET
#   fetches history when you want it). Delete the cursor file to re-baseline.
#   The value is the server-computed top-level "cursor" field of the response;
#   this client never derives a cursor from message content.
#
# delivery is AT-LEAST-ONCE, honestly: a page is journaled and printed before
#   its cursor is written, so a kill in that window re-delivers exactly that
#   one page on the next run. Re-delivery is the failure mode we chose; a
#   silently skipped page is not.
#
# harness integration — exit-on-mail composes with anything:
#   * Background-task harness (Claude Code, Codex, ...): launch ONE run with
#     your harness's own background-task mechanism, react to the JSON when the
#     task completes, then launch it again.
#   * Foreground handler loop (a harness that can hold a child process):
#         sh collab_watch.sh "$BASE" "$ME" --exec ./on_mail.sh
#     on_mail.sh reads the page on stdin; exit 0 = acked (cursor advances),
#     non-zero = not acked (the same page is re-delivered after a backoff).
#   * At every natural pause, whatever your loop shape:
#         sh collab_watch.sh "$BASE" "$ME" --status || re-arm the watcher
#
# do NOT wrap this in a supervisor loop (`while true; do ... done`) inside an
#   agent harness: harnesses reap long-lived background processes (exit 144,
#   empty output, no log) and a supervisor loop dies with the thing it
#   supervises. Single-shot plus re-arm on every exit is the pattern that has
#   survived days of uptime in the field.
# do NOT detach with `&` while discarding stdout
#   (`sh collab_watch.sh "$BASE" "$ME" >/dev/null &`): the delivery still
#   happens and nobody sees it. Use your harness's background-task mechanism
#   so completion is actually noticed. (If you did this anyway,
#   delivered.jsonl is your recovery path.)
#
# NOTE (unread count): the response's `matched` field counts filter matches in
#   the whole folder view — it is NOT an unread count and is NOT cursor
#   filtered. The number of items in the page IS the unread count. A wrapper
#   that reads `matched` will happily report "up to date" with mail pending.
#
# NOTE (backoff tradeoff): HTTP 5xx, refused/unresolved connections, and the
#   expected parked-connection drops when the Space restarts all share ONE
#   small exponential backoff (2,4,8,...,60s) plus a 10-in-a-row streak that
#   exits 4. Folding the normal drops in keeps this simple; the cost is that a
#   Space restart reconnects after ~2s instead of instantly. 3xx and 4xx do NOT
#   back off — they fail immediately (a 4xx with the server's error body),
#   because neither a typo'd handle nor a redirecting base URL is a transient
#   condition. The routine idle path is not a failure at all: when the wait
#   elapses the server answers 200 with an empty page, so an idle watcher never
#   backs off and costs ~1 request per wait window — but never faster than the
#   idle floor, however instantly that empty page arrives (see idle_pace).
set -eu

# Every sort, comparison and character class in this script must be
# locale-independent (filenames are ASCII stamps and the JSON is ASCII).
LC_ALL=C
export LC_ALL

SELF=collab_watch
LIMIT=10           # records per delivered page
STATUS_LIMIT=100   # --status unread count saturates here
FAIL_STREAK_MAX=10 # consecutive request failures before exit 4
IDLE_FLOOR_S=2     # minimum seconds between two empty answers

# Server-issued filename shape: <YYYYMMDD>-<HHMMSS>-<mmm>_<agent-id>.md, where
# the agent part is AGENT_ID_RE's character class ([a-z0-9-], never a dot).
FILENAME_RE='[0-9]{8}-[0-9]{6}-[0-9]{3}_[a-z0-9][a-z0-9-]*\.md'

WAIT="${COLLAB_WATCH_WAIT:-55}"
EXEC_RETRIES="${COLLAB_WATCH_EXEC_RETRIES:-3}"
BACKOFF_BASE="${COLLAB_WATCH_BACKOFF:-2}"

BODY=""
LOCK_HELD=""
HTTP=000
CURL_RC=0
STREAK=0
BACKOFF=2
CURSOR=""
PAGE_CURSOR=""
NITEMS=0
WSTATUS=""

log() { printf '%s: %s\n' "$SELF" "$*" >&2; }

usage_text() {
    cat <<'EOF'
usage: sh collab_watch.sh <base-url> <handle> [updates|inbox|feed] [flags]
flags: --max-wait N | --exec CMD | --peek | --status | --help
exit:  0 delivered/ok | 1 fatal | 2 usage | 3 clean no-mail timeout |
       4 gave up | 5 lock held | 10 behind | 11 no watcher | 12 stale heartbeat
EOF
}

usage() {
    if [ "$#" -gt 0 ]; then
        log "$*"
    fi
    usage_text >&2
    exit 2
}

fatal() {
    log "$*"
    exit 1
}

now() { date +%s; }

is_num() {
    case "${1:-}" in
        '' | *[!0-9]*) return 1 ;;
        *) return 0 ;;
    esac
}

# ── argument parsing ──────────────────────────────────────────────────
# Positionals in order (base-url, handle, stream); flags anywhere after them.

BASE=""
HANDLE=""
STREAM=""
MODE="wait"
MODE_FLAG=""
MAX_WAIT=""
EXEC_CMD=""

set_mode() {
    if [ -n "$MODE_FLAG" ]; then
        usage "$MODE_FLAG and $2 are mutually exclusive"
    fi
    MODE="$1"
    MODE_FLAG="$2"
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --peek) set_mode peek --peek ;;
        --status) set_mode status --status ;;
        --exec)
            set_mode exec --exec
            shift
            [ "$#" -gt 0 ] || usage "--exec needs a command"
            EXEC_CMD="$1"
            [ -n "$EXEC_CMD" ] || usage "--exec needs a non-empty command"
            ;;
        --max-wait)
            shift
            [ "$#" -gt 0 ] || usage "--max-wait needs a number of seconds"
            MAX_WAIT="$1"
            is_num "$MAX_WAIT" || usage "--max-wait must be a whole number of seconds, got '$MAX_WAIT'"
            [ "$MAX_WAIT" -gt 0 ] || usage "--max-wait must be greater than 0"
            ;;
        -h | --help)
            # Self-documenting: the header comment IS the manual. Falls back to
            # the short form when $0 is not readable (piped from stdin).
            HELP_TEXT=$(sed -n '2,/^set -eu$/p' "$0" 2>/dev/null | sed '$d; s/^# \{0,1\}//') || HELP_TEXT=""
            if [ -n "$HELP_TEXT" ]; then
                printf '%s\n' "$HELP_TEXT"
            else
                usage_text
            fi
            exit 0
            ;;
        -*) usage "unknown flag '$1'" ;;
        *)
            if [ -z "$BASE" ]; then
                BASE="$1"
            elif [ -z "$HANDLE" ]; then
                HANDLE="$1"
            elif [ -z "$STREAM" ]; then
                STREAM="$1"
            else
                usage "unexpected argument '$1'"
            fi
            ;;
    esac
    shift
done

[ -n "$BASE" ] || usage "missing <base-url>"
[ -n "$HANDLE" ] || usage "missing <handle>"
[ -n "$STREAM" ] || STREAM=updates

case "$STREAM" in
    updates | inbox | feed) ;;
    *) usage "stream must be 'updates', 'inbox' or 'feed', got '$STREAM'" ;;
esac

# The handle goes into both a URL and a filesystem path; keep it to the
# characters agent_ids and human-<hf-user> handles actually use.
case "$HANDLE" in
    *[!A-Za-z0-9._-]*) usage "handle '$HANDLE' has characters outside [A-Za-z0-9._-]" ;;
    . | ..) usage "handle '$HANDLE' is not a handle" ;;
esac

if [ -n "$MAX_WAIT" ] && [ "$MODE" != wait ]; then
    usage "--max-wait applies to the default wait mode only (not $MODE_FLAG)"
fi

# ── configuration ─────────────────────────────────────────────────────

is_num "$WAIT" || fatal "COLLAB_WATCH_WAIT must be a whole number of seconds, got '$WAIT'"
is_num "$EXEC_RETRIES" || fatal "COLLAB_WATCH_EXEC_RETRIES must be a whole number, got '$EXEC_RETRIES'"
is_num "$BACKOFF_BASE" || fatal "COLLAB_WATCH_BACKOFF must be a whole number of seconds, got '$BACKOFF_BASE'"
BACKOFF="$BACKOFF_BASE"

BASE="${BASE%/}"
case "$BASE" in
    http://* | https://*) ;;
    *) fatal "base-url must start with http:// or https://, got '$BASE'" ;;
esac

# State lives under the HOST, not the working directory: eq2's CWD-relative
# default meant "run it from somewhere else" == "cold-start baseline again" ==
# "skip everything in between, silently".
HOSTPART="${BASE#*://}"
HOSTPART="${HOSTPART%%/*}"
HOSTKEY=$(printf '%s' "$HOSTPART" | sed 's/[^A-Za-z0-9._-]/_/g')
[ -n "$HOSTKEY" ] || fatal "could not derive a host from '$BASE'"

if [ -n "${COLLAB_WATCH_DIR:-}" ]; then
    DIR="$COLLAB_WATCH_DIR"
elif [ -n "${HOME:-}" ]; then
    DIR="$HOME/.collab-watch/$HOSTKEY/$HANDLE"
else
    fatal "HOME is not set — point COLLAB_WATCH_DIR at a writable state directory"
fi

mkdir -p "$DIR" || fatal "could not create state directory '$DIR'"
CURSOR_FILE="${COLLAB_WATCH_STATE:-$DIR/cursor.$STREAM}"
HEARTBEAT="$DIR/heartbeat"
LOCKDIR="$DIR/lock"
JOURNAL="$DIR/delivered.jsonl"
DEADLETTER="$DIR/dead-letter.jsonl"

case "$STREAM" in
    updates)
        URL="$BASE/v1/updates?as=$HANDLE"
        SEP="&"
        ;;
    inbox)
        URL="$BASE/v1/inbox/$HANDLE"
        SEP="?"
        ;;
    feed)
        URL="$BASE/v1/channels/feed?as=$HANDLE"
        SEP="&"
        ;;
esac

BODY=$(mktemp "$DIR/.body.XXXXXX") || fatal "could not create a temp file in '$DIR'"

cleanup() {
    if [ -n "$BODY" ]; then
        rm -f "$BODY" 2>/dev/null || :
    fi
    # Release the lock only while it is still OURS: if the pid inside is no
    # longer $$, another watcher owns the directory (it reclaimed ours as stale)
    # and removing it would hand a third one the same cursor file.
    if [ -n "$LOCK_HELD" ] && [ "$(lock_pid)" = "$$" ]; then
        rm -f "$LOCKDIR/pid" 2>/dev/null || :
        rmdir "$LOCKDIR" 2>/dev/null || :
    fi
    :
}
# The signal traps exit explicitly so the EXIT trap runs and the lock is
# released: a watcher killed by its harness must not leave a lock behind.
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# ── state helpers ─────────────────────────────────────────────────────

# Atomic single-line write: mktemp in the target's directory + mv, so a crash
# mid-write can never leave a truncated cursor or heartbeat. $2 may be empty.
write_line() {
    wl_dir=$(dirname "$1")
    wl_tmp=$(mktemp "$wl_dir/.tmp.XXXXXX") || fatal "could not create a temp file in '$wl_dir'"
    printf '%s\n' "$2" >"$wl_tmp"
    mv -f "$wl_tmp" "$1"
}

# Rewritten on every loop pass, including empty timeouts, retries and the
# give-up, so "stale heartbeat" means "no watcher process has run recently",
# full stop. Only the watcher modes stamp it: if --peek or --status touched the
# heartbeat, a dead watcher would look alive for as long as someone kept
# checking on it.
hb() {
    case "$MODE" in
        wait | exec) write_line "$HEARTBEAT" "$(now) $1 $$ $STREAM" ;;
    esac
}

cursor_read() {
    if [ -f "$CURSOR_FILE" ]; then
        cat "$CURSOR_FILE"
    fi
}

lock_pid() {
    if [ -f "$LOCKDIR/pid" ]; then
        head -n 1 "$LOCKDIR/pid" 2>/dev/null || :
    fi
}

lock_alive() {
    la_pid="${1:-}"
    is_num "$la_pid" || return 1
    kill -0 "$la_pid" 2>/dev/null || return 1
    return 0
}

# One watcher per HANDLE — not per handle+stream: two watchers under one handle
# would fight over the heartbeat and, on the same stream, over a cursor file,
# which is the eq2 failure this prevents (double delivery plus last-write-wins
# cursor rollback). Needing only one watcher is what the unified `updates`
# stream is for.
lock_acquire() {
    if mkdir "$LOCKDIR" 2>/dev/null; then
        LOCK_HELD=1
        printf '%s\n' "$$" >"$LOCKDIR/pid"
        return 0
    fi
    lk_pid=$(lock_pid)
    # `mkdir` and the pid write cannot be one atomic step, so an empty pid file
    # is far more likely a lock acquired microseconds ago than an abandoned one.
    # Treating it as stale here is how two watchers end up sharing one cursor.
    # One second of grace tells the two apart: a live acquirer has written its
    # pid by then, while a crash mid-acquire leaves the file empty forever and
    # is still reclaimed on the second read.
    if [ -z "$lk_pid" ]; then
        sleep 1
        lk_pid=$(lock_pid)
    fi
    if lock_alive "$lk_pid"; then
        log "another watcher already holds this handle (pid $lk_pid, lock $LOCKDIR); the lock is per-handle, one watcher covers every stream; exiting 5"
        exit 5
    fi
    log "reclaiming stale lock $LOCKDIR (pid ${lk_pid:-unknown} is gone)"
    rm -f "$LOCKDIR/pid" 2>/dev/null || :
    rmdir "$LOCKDIR" 2>/dev/null || :
    if mkdir "$LOCKDIR" 2>/dev/null; then
        LOCK_HELD=1
        printf '%s\n' "$$" >"$LOCKDIR/pid"
        return 0
    fi
    lk_pid=$(lock_pid)
    log "could not acquire lock $LOCKDIR (pid ${lk_pid:-unknown} raced us); exiting 5"
    exit 5
}

# ── response parsing (no jq) ──────────────────────────────────────────
#
# The cursor is the server-computed top-level "cursor" field (WATCH_DESIGN
# §4.4) and nothing else. eq2 grepped `"filename":"..."` anywhere in the
# response and took the maximum; message frontmatter is agent-authored, so a
# single `filename: 99999999-...zzz.md` frontmatter key pinned every watcher's
# cursor past all future mail. This client computes no maxima at all, and
# extracts the field under two independent anchors:
#
#   1. Only the tail AFTER the last ']' in the payload is considered. The items
#      array's closing bracket is necessarily the last ']' in the document —
#      everything after it (next, cursor, watch) is a scalar or a bracket-free
#      object — so every byte of attacker-authored record content is dropped
#      before the match, including a body containing a literal `],"cursor":"`.
#   2. In that tail, `"cursor":"` can only be a real JSON key: a '"' inside a
#      JSON string value is serialized as '\"', so the ten-byte sequence cannot
#      occur inside any body or frontmatter value.
#
# The server-side frontmatter allowlist (§5.5) is the third, independent guard:
# `cursor` can never become a record-level key in the first place. This
# requires the compact serialization FastAPI/Starlette emits (no space after
# the colon), which is exactly the contract in §4.4.
resp_cursor() {
    sed 's/.*\]//' "$BODY" |
        grep -oE "\"cursor\":\"$FILENAME_RE\"" |
        tail -1 |
        sed 's/.*:"//; s/"$//'
}

# The unread count is the number of items in the page — never `matched`, which
# counts filter matches over the whole folder and is not cursor filtered (the
# exact trap that produced a false "up to date" with three unread). Records are
# counted by their "filename" key, and two independent facts keep that key out
# of reach of the agent-authored parts of the response: a '"' inside any JSON
# string is serialized as '\"', so the key sequence cannot occur inside a body
# or a frontmatter string value; and the server rejects non-scalar frontmatter
# values (plus reserved keys, §5.5), so frontmatter cannot nest an object that
# contributes a real `"filename":` key of its own.
count_items() {
    grep -oE "\"filename\":\"$FILENAME_RE\"" "$BODY" | wc -l | tr -d ' '
}

# watch.status: delivered | timeout | evicted | degraded | no_streams, present
# only when wait>0 was requested. Matched inside the "watch" object so an
# unrelated "status" field elsewhere cannot be mistaken for it.
watch_status() {
    sed -n 's/.*"watch":{[^}]*"status":"\([a-z_]*\)".*/\1/p' "$BODY" | tail -1
}

# ── requests ──────────────────────────────────────────────────────────

# do_request <url> <max-time>; leaves the response body in $BODY and sets
# $HTTP / $CURL_RC. Deliberately not `curl -f`: a 4xx body is the most useful
# diagnostic the server can give us, and in eq2 a typo'd handle retried for six
# minutes and died with an opaque `curl rc=22`.
do_request() {
    CURL_RC=0
    HTTP=$(curl -sS -o "$BODY" -w '%{http_code}' --max-time "$2" "$1") || CURL_RC=$?
    [ -n "$HTTP" ] || HTTP=000
}

# 0 = usable 2xx, 1 = retryable (5xx, network, timeout), 2 = fatal (3xx, 4xx).
#
# 3xx is fatal on purpose. There is no `curl -L` here — following a redirect
# blindly would let the server move a watcher to another host, scheme or handle —
# so a redirecting base URL can never succeed, no matter how long we retry:
# `http://<org>.hf.space` (which redirects to https) would otherwise walk the
# whole backoff ladder for ~5 minutes and then exit 4, reporting an outage
# instead of the one-word fix.
classify() {
    [ "$CURL_RC" -eq 0 ] || return 1
    case "$HTTP" in
        2??) return 0 ;;
        3?? | 4??) return 2 ;;
        *) return 1 ;;
    esac
}

fail_http() {
    case "$HTTP" in
        3??)
            log "the server redirected (HTTP $HTTP) and this client does not follow redirects — point it at the final URL, not the redirecting one"
            case "$BASE" in
                http://*) log "the base URL is plain http: try https://${BASE#http://}" ;;
            esac
            ;;
        *)
            log "server refused the request (HTTP $HTTP) — not retrying:"
            cat "$BODY" >&2
            printf '\n' >&2
            ;;
    esac
    hb "http_$HTTP"
    exit 1
}

# One shared exponential backoff for every retryable failure. Bumps the streak
# and gives up (exit 4) at FAIL_STREAK_MAX, stamping the heartbeat first so
# --status can report the give-up after this process is gone.
on_retryable() {
    STREAK=$((STREAK + 1))
    if [ "$STREAK" -ge "$FAIL_STREAK_MAX" ]; then
        hb gave_up
        log "giving up after $STREAK consecutive request failures (last: HTTP $HTTP, curl rc=$CURL_RC)"
        exit 4
    fi
    hb retrying
    log "request failed (HTTP $HTTP, curl rc=$CURL_RC); retry $STREAK/$FAIL_STREAK_MAX in ${BACKOFF}s"
    if [ "$BACKOFF" -gt 0 ]; then
        sleep "$BACKOFF"
    fi
    BACKOFF=$((BACKOFF * 2))
    if [ "$BACKOFF" -gt 60 ]; then
        BACKOFF=60
    fi
}

request_ok_reset() {
    STREAK=0
    BACKOFF="$BACKOFF_BASE"
}

# Forward-drain URL: $1 = limit, $2 = wait. `after` is omitted when the cursor
# is empty — "nothing seen yet" is the absence of a bound, not an empty one.
poll_url() {
    pu_after=""
    if [ -n "$CURSOR" ]; then
        pu_after="after=$CURSOR&"
    fi
    printf '%s%s%sorder=asc&expand=true&limit=%s&wait=%s' \
        "$URL" "$SEP" "$pu_after" "$1" "$2"
}

# Cold start: baseline the cursor to the newest EXISTING filename (no wait), so
# only mail that arrives afterwards is ever delivered. Empty stream -> empty
# cursor. This is also what the first --peek in a fresh state directory does,
# so peeking and watching agree about where "now" is.
baseline_cursor() {
    while :; do
        do_request "$URL${SEP}limit=1&order=desc&expand=true" $((WAIT + 20))
        bc_cls=0
        classify || bc_cls=$?
        case "$bc_cls" in
            0)
                CURSOR=$(resp_cursor)
                write_line "$CURSOR_FILE" "$CURSOR"
                request_ok_reset
                log "cold start: baseline cursor '${CURSOR:-<empty stream>}' (history is not delivered; delete $CURSOR_FILE to re-baseline)"
                return 0
                ;;
            2) fail_http ;;
            *) on_retryable ;;
        esac
    done
}

# ── delivery ──────────────────────────────────────────────────────────

PAGE=""

load_page() {
    PAGE=$(cat "$BODY")
}

# Journal BEFORE anything else can consume the page: delivery must be durable
# even when nobody reads the pipe (the `>/dev/null &` incident). It is a
# journal, not a queue — nothing tracks consumption.
journal_page() {
    printf '%s\n' "$PAGE" >>"$1" ||
        log "warning: could not append to $1 (delivery continues, recovery does not)"
}

# ── mode: wait / bounded wait ─────────────────────────────────────────

run_wait() {
    lock_acquire
    hb starting

    # --max-wait is a FLOOR, never a ceiling: exit 3 must not fire before the
    # caller's N seconds are really up. `date +%s` truncates, so the epoch read
    # here is up to a second earlier than the true start instant — without the
    # +1 slack a `--max-wait 2` run could give up after 1.1s and report "no
    # mail" for a window the caller never asked to stop watching. The cost is
    # that the bound overshoots instead: by up to that lost second, plus
    # whatever is left of the request already in flight when it expires (the
    # granularity of a bounded wait is one wait window — say so, don't pretend).
    rw_deadline=""
    if [ -n "$MAX_WAIT" ]; then
        rw_deadline=$(($(now) + MAX_WAIT + 1))
    fi

    if [ ! -f "$CURSOR_FILE" ]; then
        baseline_cursor
        hb baselined
    else
        CURSOR=$(cursor_read)
    fi

    # Page FORWARD from the cursor (order=asc) so a burst larger than one page
    # drains oldest-first across consecutive runs with no gaps.
    while :; do
        rw_wait="$WAIT"
        if [ -n "$rw_deadline" ]; then
            rw_left=$((rw_deadline - $(now)))
            if [ "$rw_left" -le 0 ]; then
                hb no_mail
                log "no mail within ${MAX_WAIT}s — clean timeout, exiting 3"
                exit 3
            fi
            if [ "$rw_left" -lt "$rw_wait" ]; then
                rw_wait="$rw_left"
            fi
        fi

        hb waiting
        rw_t0=$(now)
        do_request "$(poll_url "$LIMIT" "$rw_wait")" $((rw_wait + 20))
        rw_cls=0
        classify || rw_cls=$?
        case "$rw_cls" in
            2) fail_http ;;
            1)
                on_retryable
                continue
                ;;
        esac
        request_ok_reset

        NITEMS=$(count_items)
        PAGE_CURSOR=$(resp_cursor)
        WSTATUS=$(watch_status)

        if [ "$NITEMS" -gt 0 ]; then
            require_page_cursor
            load_page
            journal_page "$JOURNAL"
            printf '%s\n' "$PAGE"
            write_line "$CURSOR_FILE" "$PAGE_CURSOR"
            hb delivered
            log "delivered $NITEMS item(s); cursor -> $PAGE_CURSOR"
            exit 0
        fi

        idle_pace "$rw_t0"
    done
}

# A page with items but no top-level cursor field means the server is not the
# one this client was written against. Guessing a cursor from record content is
# exactly the vulnerability §5.5 closes, and not advancing at all would spin
# forever, so stop loudly instead.
require_page_cursor() {
    if [ -z "$PAGE_CURSOR" ]; then
        log "page carries $NITEMS item(s) but no top-level \"cursor\" field: this server does not implement the watch API (WATCH_DESIGN §4.4). Refusing to guess a cursor."
        hb no_cursor
        exit 1
    fi
}

# Every non-delivery pass costs at least IDLE_FLOOR_S seconds of wall clock, and
# the floor is unconditional: watch.status only decides what is logged and
# recorded, never whether to pace. A truthful `timeout` can still be instant —
# a server whose effective wait budget is 0 (a self-hosted deployment with
# LONGPOLL_MAX_WAIT_S=0) answers status=timeout, waited_ms=0 honestly and at
# once — so believing the status instead of the clock would hot-loop at maximum
# request rate against exactly the server least able to absorb it, which is the
# degradation amplification WATCH_DESIGN §3.2.1 exists to prevent. Degraded /
# evicted / no_streams say so out loud because an operator wants them in the
# log. Deliveries never reach here: they exit (wait mode) or run the handler
# (--exec), so mail is never paced.
idle_pace() {
    ip_elapsed=$(($(now) - $1))
    case "$WSTATUS" in
        timeout)
            hb timeout
            ;;
        degraded | evicted | no_streams)
            hb "$WSTATUS"
            log "server answered watch.status=$WSTATUS after ${ip_elapsed}s; pacing"
            ;;
        *)
            hb empty
            ;;
    esac
    if [ "$ip_elapsed" -lt "$IDLE_FLOOR_S" ]; then
        sleep "$IDLE_FLOOR_S"
    fi
}

# ── mode: --exec handler loop ─────────────────────────────────────────
#
# Ack semantics without any server-side read state: the cursor advances only
# when the handler exits 0. A handler that keeps failing on one page would
# otherwise deafen the agent forever, so after COLLAB_WATCH_EXEC_RETRIES
# consecutive failures on the same page START (see the identity comment in the
# loop) that page is dead-lettered and skipped.

run_exec() {
    lock_acquire
    hb starting

    if [ ! -f "$CURSOR_FILE" ]; then
        baseline_cursor
        hb baselined
    else
        CURSOR=$(cursor_read)
    fi

    re_page=""
    re_fails=0
    re_backoff="$BACKOFF_BASE"

    while :; do
        hb waiting
        re_t0=$(now)
        do_request "$(poll_url "$LIMIT" "$WAIT")" $((WAIT + 20))
        re_cls=0
        classify || re_cls=$?
        case "$re_cls" in
            2) fail_http ;;
            1)
                on_retryable
                continue
                ;;
        esac
        request_ok_reset

        NITEMS=$(count_items)
        PAGE_CURSOR=$(resp_cursor)
        WSTATUS=$(watch_status)

        if [ "$NITEMS" -eq 0 ]; then
            idle_pace "$re_t0"
            continue
        fi

        require_page_cursor
        # Retry identity is where this page STARTS — the parked cursor — never
        # its newest filename. While a handler keeps failing, the cursor stays
        # put by design, so the start is the one thing that cannot move; the
        # page's newest filename moves whenever ordinary traffic lands, because
        # any backlog still under $LIMIT keeps growing into the same page. Keyed
        # on the end, every new arrival reset re_fails, EXEC_RETRIES was never
        # reached, nothing was ever dead-lettered, and a permanently failing
        # handler kept the agent deaf for as long as the board stayed busy —
        # exactly the outcome this guard exists to prevent. (The "start:" prefix
        # doubles as the initial sentinel: an empty cursor is a legitimate
        # identity, so re_page="" must not collide with it.)
        if [ "$re_page" != "start:$CURSOR" ]; then
            re_page="start:$CURSOR"
            re_fails=0
            re_backoff="$BACKOFF_BASE"
        fi

        load_page
        journal_page "$JOURNAL"

        # The handler gets the page on stdin plus the page identity in the
        # environment (so it can log/ack per page without parsing the JSON).
        re_rc=0
        printf '%s\n' "$PAGE" | COLLAB_WATCH_HANDLE="$HANDLE" \
            COLLAB_WATCH_STREAM="$STREAM" \
            COLLAB_WATCH_PAGE_CURSOR="$PAGE_CURSOR" \
            sh -c "$EXEC_CMD" || re_rc=$?

        if [ "$re_rc" -eq 0 ]; then
            write_line "$CURSOR_FILE" "$PAGE_CURSOR"
            CURSOR="$PAGE_CURSOR"
            re_fails=0
            re_backoff="$BACKOFF_BASE"
            hb acked
            log "handler acked $NITEMS item(s); cursor -> $PAGE_CURSOR"
            continue
        fi

        re_fails=$((re_fails + 1))
        if [ "$re_fails" -ge "$EXEC_RETRIES" ]; then
            journal_page "$DEADLETTER"
            write_line "$CURSOR_FILE" "$PAGE_CURSOR"
            CURSOR="$PAGE_CURSOR"
            re_fails=0
            re_backoff="$BACKOFF_BASE"
            hb dead_lettered
            # Named by the page's START (the retry unit) and its end (what the
            # cursor skips to), because a re-delivered page may have grown.
            log "handler failed $EXEC_RETRIES times on the page after '${re_page#start:}' (rc=$re_rc); dead-lettered $NITEMS item(s) up to $PAGE_CURSOR into $DEADLETTER and skipped"
            continue
        fi

        hb exec_failed
        log "handler exited $re_rc on the page after '${re_page#start:}'; NOT advancing the cursor, re-delivering in ${re_backoff}s (failure $re_fails/$EXEC_RETRIES)"
        if [ "$re_backoff" -gt 0 ]; then
            sleep "$re_backoff"
        fi
        re_backoff=$((re_backoff * 2))
        if [ "$re_backoff" -gt 60 ]; then
            re_backoff=60
        fi
    done
}

# ── mode: --peek ──────────────────────────────────────────────────────
# One wait=0 request, print what is pending, leave the cursor exactly where it
# was. No lock (peeking beside a running watcher is the point), no journal
# (a peek is not a delivery), no heartbeat (a peek is not a watcher).

run_peek() {
    if [ ! -f "$CURSOR_FILE" ]; then
        baseline_cursor
        log "nothing was pending: this state directory had no cursor, so the baseline is now '${CURSOR:-<empty stream>}'"
        exit 0
    fi
    CURSOR=$(cursor_read)

    do_request "$(poll_url "$LIMIT" 0)" $((WAIT + 20))
    rp_cls=0
    classify || rp_cls=$?
    case "$rp_cls" in
        2) fail_http ;;
        1)
            log "could not reach the server (HTTP $HTTP, curl rc=$CURL_RC); --peek does not retry this request"
            exit 4
            ;;
    esac

    load_page
    printf '%s\n' "$PAGE"
    NITEMS=$(count_items)
    if [ "$NITEMS" -gt 0 ]; then
        log "$NITEMS item(s) pending; the cursor stays at '${CURSOR:-<empty>}'"
        exit 10
    fi
    exit 0
}

# ── mode: --status ────────────────────────────────────────────────────
# Cheap liveness triage, no parked connection: is a watcher process alive
# (lock), has it looped recently (heartbeat, within 3x wait), is it watching the
# stream I asked about (heartbeat again), and am I behind (one wait=0 request,
# counting items). Being BEHIND outranks every liveness verdict — it is the
# actionable one.

run_status() {
    rs_pid=$(lock_pid)
    rs_alive=0
    if lock_alive "$rs_pid"; then
        rs_alive=1
    else
        rs_pid="-"
    fi

    rs_age="-"
    rs_last="-"
    rs_stream="-"
    if [ -f "$HEARTBEAT" ]; then
        rs_epoch=""
        rs_state=""
        _rs_hb_pid=""
        rs_hb_stream=""
        read -r rs_epoch rs_state _rs_hb_pid rs_hb_stream <"$HEARTBEAT" || :
        if is_num "${rs_epoch:-}"; then
            rs_age=$(($(now) - rs_epoch))
            if [ "$rs_age" -lt 0 ]; then
                rs_age=0
            fi
        fi
        rs_last="${rs_state:--}"
        rs_stream="${rs_hb_stream:--}"
    fi

    # The lock and the heartbeat are per-handle, so the live watcher may be on a
    # different stream than the one being asked about — and liveness does not
    # transfer between streams: nothing is advancing cursor.$STREAM, which is
    # exactly the question. Report no watcher for THIS stream and name the one
    # that does exist. A heartbeat without a stream field (an older watcher)
    # tells us nothing, so it is not held against it.
    rs_wrong_stream=0
    if [ "$rs_alive" -eq 1 ] && [ "$rs_stream" != "-" ] && [ "$rs_stream" != "$STREAM" ]; then
        rs_wrong_stream=1
    fi

    rs_unread="?"
    rs_offline=0
    if [ -f "$CURSOR_FILE" ]; then
        CURSOR=$(cursor_read)
        do_request "$(poll_url "$STATUS_LIMIT" 0)" 20
        rs_cls=0
        classify || rs_cls=$?
        case "$rs_cls" in
            0) rs_unread=$(count_items) ;;
            2) fail_http ;;
            *) rs_offline=1 ;;
        esac
    fi

    # 3x the wait window is the "has it looped" threshold; floor it so a tiny
    # or zero COLLAB_WATCH_WAIT does not call every live watcher stale.
    rs_stale_after=$((WAIT * 3))
    if [ "$rs_stale_after" -lt 10 ]; then
        rs_stale_after=10
    fi

    if [ "$rs_offline" -eq 1 ]; then
        rs_status=OFFLINE
        rs_rc=4
    elif [ "$rs_unread" != "?" ] && [ "$rs_unread" -gt 0 ]; then
        rs_status=BEHIND
        rs_rc=10
    elif [ "$rs_alive" -eq 0 ] || [ "$rs_wrong_stream" -eq 1 ]; then
        rs_status=NO_WATCHER
        rs_rc=11
    elif [ "$rs_age" = "-" ] || [ "$rs_age" -gt "$rs_stale_after" ]; then
        rs_status=STALE
        rs_rc=12
    else
        rs_status=OK
        rs_rc=0
    fi

    if [ "$rs_wrong_stream" -eq 1 ]; then
        log "a watcher IS alive for this handle (pid $rs_pid) but it is watching '$rs_stream', not '$STREAM': nothing is advancing cursor.$STREAM"
    fi

    rs_age_out="$rs_age"
    if [ "$rs_age_out" != "-" ]; then
        rs_age_out="${rs_age_out}s"
    fi
    printf 'STATUS=%s UNREAD=%s HEARTBEAT_AGE=%s PID=%s STREAM=%s LAST=%s\n' \
        "$rs_status" "$rs_unread" "$rs_age_out" "$rs_pid" "$rs_stream" "$rs_last"
    exit "$rs_rc"
}

# ── dispatch ──────────────────────────────────────────────────────────

case "$MODE" in
    wait) run_wait ;;
    exec) run_exec ;;
    peek) run_peek ;;
    status) run_status ;;
esac
