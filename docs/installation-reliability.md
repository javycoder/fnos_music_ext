# Installation and socket recovery safety

## Contracts

`proxy/takeover.py` is the shared Linux socket supervisor/state helper used by
`extend.sh`, `restore.sh`, and the canonical `fnmusic-ext.service` template.
The scripts explicitly invoke Bash/Python, including with a 0644 checkout.

* Official identity requires Linux `SO_PEERCRED` and a `/proc/<pid>/exe` basename
  of `trim-music`. PID, process start time, executable, device and inode are
  recorded. Business responses such as `INVALID TOKEN` are never identity.
* Proxy identity requires HTTP 200 JSON from `/_ext/livez`, with
  `service: "fnmusic-ext"` and `pid` equal to the connected kernel peer PID.
* Readiness additionally requires the recorded proxy inode/process and
  `/_ext/healthz` with `ok: true`, `upstream: "ok"`. Each request allows 4 seconds
  (the app readiness budget is approximately 2.5 seconds). The helper uses a
  monotonic overall deadline, including connection/read time. Source curl loops
  likewise have an outer GNU `timeout`, with 4-second individual requests.

## Startup and shutdown

The project `.venv-proxy/bin/python -m uvicorn` is mandatory: no global fallback.
Dotenv is parsed as quoted data by the supervisor, not executed as shell.
A temporary-home import preflight redirects all known proxy storage paths and
DB/socket paths; bytecode writes are disabled. Numeric/import errors fail before
moving any socket. The real child first binds a private staging socket. Only after
its liveness PID is verified is the official socket moved and the staged proxy
published. Moves use Linux `renameat2(RENAME_NOREPLACE)`, refusing to overwrite an
unexpected concurrent path. Missing kernel/libc support fails conservatively.

`/run/fnmusic-ext/ownership.json` is a 0600 atomic journal in a private directory.
It is written before takeover. It also enables recovery of the exact dead proxy
inode when no HTTP probe is possible. Normal stop, SIGTERM/SIGINT, child crashes,
and readiness failures stop/reap the child and verify official restoration.
`ExecStopPost` retries the same recovery after systemd termination. Automatic
restart is deliberately disabled to avoid repeated disruptive failed takeovers.

Exception classes, missing dependency names and stage/exit status are reported,
not arbitrary config values or response bodies. A small allowlist also preserves
child startup/shutdown messages, source/search/stream/auth/recommend failure
outcomes, known timeout/error categories, LLM HTTP status, timestamps/levels and
traceback module/function/line locations. Appended exception payloads, URLs,
credentials, full paths, configuration dumps and unrecognized messages are omitted;
configured secret values are removed before filtering. Oversized physical lines
are discarded in full. HTTP access logs remain disabled. This is deliberately
not a general-purpose redactor: new operational message formats need an allowlist
entry and a regression test. Preflight is not an OS sandbox: it isolates the current
application's known storage paths, not arbitrary future import side effects.

## Locks and ownership boundaries

* The outer installer `flock` serializes install/extend/restore. Its parent keeps
  the lock across install-to-extend chaining; file descriptors are closed in the
  invoked child. Root only prepares the stable lock inode; installer uid, HOME
  and environment remain those of the caller. Services never acquire this lock.
* A separate supervisor lifetime lock prevents duplicate supervisors.
* Short socket mutation locks protect the journal/moves. They are never held
  across `systemctl` or across service lifetime, avoiding installer/PID1 deadlocks.
* A proxy unit from another checkout is not stopped/overwritten. Container
  reconciliation/removal requires an exact Compose working-directory label;
  unknown/foreign containers are preserved. Source unit cleanup also checks the
  checkout's working directory. Host selected units are enabled then explicitly
  restarted; restart/readiness failures are errors, not successful installs.

## Conservative recovery limits

Unknown live sockets, symlinks/non-sockets, two official listeners, changed
recorded ownership, and unrecorded dead sockets are preserved and cause failure.
A legacy layout with an absent target and a positively identified official
upstream can be restored. A legacy live proxy with the new identity endpoint can
be recorded before shutdown. Older proxies without that endpoint cannot be
identified from forwarded business responses and may require restoration from
the original checkout or an administrator-controlled official application restart.
A reboot loses `/run` records; dead unrecorded sockets are never guessed away.
SIGKILL of the supervisor relies on systemd killing the remaining child and
`ExecStopPost`; invoking the supervisor outside systemd does not provide that
external crash handler. Cooperative locks cannot fully serialize an unrelated
root process mutating the same paths. No-replace moves and repeated identity
checks reduce races; do not concurrently restart the official application during
installation or recovery.

On an ambiguous state, stop and investigate; do not manually remove socket paths
based solely on a successful business request. Failed verification does not print
“restore succeeded”. Changes to source services/configuration are not a complete
transactional rollback of an installation; rollback covers the socket takeover.

## Isolated validation

Run `PYTHONDONTWRITEBYTECODE=1 python3 -m pytest -q -p no:cacheprovider
proxy/tests/test_installation_reliability.py`. Tests use temporary UDS subprocesses,
including a temporary Python executable named `trim-music`, exact inode/process
checks, readiness delays, child SIGKILL, supervisor SIGTERM, startup exceptions,
and all seven nonempty source combinations in host/docker modes. Config imports
use the test interpreter and temporary storage (fixture project-Python paths point
to that interpreter, so CI does not skip coverage); lifecycle assertions use mocked
commands. They do not launch Docker or install/start units under host PID1.
Offline `systemd-analyze verify` checks unit syntax only, not real PID1 behavior.
A disposable fnOS/systemd VM is still required for end-to-end deployment validation.
