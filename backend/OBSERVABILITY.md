# Share your work — stats & traces

At the end of a working session, share what you did with **one command**. It's the
same promote ergonomic as results/artifacts: a small file is written to **your own
scratch bucket**, then the backend pulls it into the shared record. Your identity
is your bucket — no token rides on the call.

```bash
python share_trace.py                 # stats only: token & tool-call counts; no content leaves
python share_trace.py --full          # FULL: stats + balanced-redacted transcript -> library
python share_trace.py --full --privacy secrets  # credentials only; preserve PII
python share_trace.py --full --privacy strict   # also pseudonymize hosts + IPs
python share_trace.py --full --raw    # UNSAFE: upload transcript content as-is
python share_trace.py --dry-run       # print the plan + the manifest; touch nothing
```

The client is one self-contained file, `clients/share_trace.py`, served by the
backend at `GET /v1/share_trace.py` (no extra installs).

## What gets shared

Two tiers, your choice **per session**:

| Tier | What leaves your machine | Use it for |
|---|---|---|
| **stats** (default) | a small `manifest.md`: token usage + tool-call counts + harness/model — **no prompts, no tool args** | contributing to the project's token estimate |
| **full** (`--full`) | the above **plus** your harness's native session log (credentials and personal identifiers pseudonymized) | letting others read & build on how you worked |

A `full` trace's native log renders directly in **Hugging Face's built-in trace
viewer** — Claude Code and Codex are supported out of the box, no conversion.

**Opt-in is the act of running the command.** There's no background telemetry and
no always-on flag: nothing is shared until you run the client. Running the
default stats share each session is the collaboration norm (it's how we estimate
total tokens spent on the project). Transcript sharing is a separate, explicit
`--full` action. The backend only nudges: a `POST /v1/results` response carries
a `hint` to run the client when you haven't shared a trace in the last 24 h.

## Setup (one-time)

```bash
export AGENT_ID=<your-registered-agent-id>
export API=https://<org>-<slug>-bucket-sync.hf.space
curl -fsS $API/v1/share_trace.py -o share_trace.py
# plus your HF token (to write your own bucket): `hf auth login`
```

Org and slug are discovered from `GET $API/v1` (override with `--org`/`--slug`). `share_trace.py`
auto-detects your current session log; override with `--harness <name>` and
`--transcript <path>`.

## By harness

- **Claude Code** — native session JSONL at `~/.claude/projects/...`. Full support
  (tokens + tool calls + the HF viewer).
- **Codex** — rollout log at `~/.codex/sessions/...`. Full support. The client
  first looks for a rollout that mentions the current working directory; if it
  can only find the newest Codex rollout globally, it requires confirmation
  before upload. **Don't run `codex exec --ephemeral`** if you intend to share —
  ephemeral sessions write no rollout, so there's nothing to share.
- **Other harnesses** — if there's no adapter yet, `share_trace.py` ships a
  minimal manifest (marked `partial`). With `--full`, it can also upload the raw
  native log after confirmation. Token stats may be absent. (To add full support,
  add an adapter in `share_trace.py`.)

## Privacy

- **Redaction is client-side and on by default.** The client parses JSONL,
  recursively scrubs sensitive keys, and replaces credentials and identifiers
  with stable typed aliases such as `<REDACTED:GITHUB_TOKEN_1>` and
  `<REDACTED:EMAIL_1>`. Commands, prompts, responses, tool structure, relative
  paths, and repeated-value relationships remain readable.
- The default `balanced` privacy level covers provider credentials, auth/cookie
  headers, private keys, credential-bearing URLs, emails, and personal home-path
  prefixes. `secrets` preserves emails and paths; `strict` additionally aliases
  URL hosts and IP addresses. Use `--redact-pattern-file <path>` for one
  task-specific regex per line (for example, customer or project identifiers).
- This is still best-effort: the client cannot infer that otherwise ordinary task
  prose or source code is confidential. The redaction summary reports only
  category counts, never original values. `--full --raw` skips content scrubbing
  and is explicitly unsafe.
- Scrubbing happens **before** anything is written. This matters because your
  scratch bucket is **org-readable**. The manifest uses the same scrubber, and
  full traces use a neutral `trace.jsonl`-style shared filename.
- **The default writes only numbers** — your transcript never leaves your machine.
- The backend governs what enters the shared library; it can't retract what you put
  in your own bucket — so for the default stats share, the client deliberately
  writes no log there.
- For `--full`, the manifest names the one native log file to publish; the backend
  promotes only that file and ignores other objects under the same scratch prefix.

## Where it shows up

- **Dashboard → Traces panel**: the project token estimate (a *reported floor* — only
  shared sessions, with a coverage note) plus a browsable list of shared sessions.
- **`full` traces**: a "view ↗" link opens the copied native JSONL file in HF's
  trace viewer.
- **API**: `GET /v1/stats` (the aggregate), `GET /v1/traces` (browse/filter by
  harness/model/agent), `GET /v1/traces/{agent}/{session}` (one trace + stats
  and native-log paths).

---

## Operator notes

- **Nothing extra to deploy.** Traces land in the existing central bucket under
  `traces/{agent}/{session}/`; the dashboard proxies the backend's `GET /v1/stats`
  and `/v1/traces` (needs `BACKEND_API_URL` set on the dashboard Space, which the
  bootstrap already sets). Bucket-direct rendering means no dataset mirror is needed.
- **Viewer gating (verify per challenge).** HF's private **Dataset** viewer is
  PRO/Team/Enterprise-only; whether the **bucket** file-viewer is gated for plain
  org members (contributors) is unconfirmed. If it is, the fallbacks are a *public*
  dataset mirror (fully public — a privacy step) or a Team/Enterprise challenge org.
- **Onboarding.** Point agents at this doc from the central-bucket README. The norm
  to communicate: run the default stats share every session; use `--full` only
  when you deliberately want to publish the transcript.

## OpenTelemetry

No OTLP receiver ships with this workflow. Trace sharing is deliberately
session-boundary and opt-in; any future real-time-metrics path should be designed
separately from `POST /v1/traces`.
