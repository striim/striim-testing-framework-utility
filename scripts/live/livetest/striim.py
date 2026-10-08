from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests

from livetest import paths

# Make tools/python/striim_api.py importable (repo layout: <repo>/tools/python/).
_TOOLS = paths.tools_python()
if str(_TOOLS) not in sys.path:
    sys.path.insert(0, str(_TOOLS))
import striim_api  # noqa: E402

TERMINAL_STATUSES = {"CRASH", "HALT", "TERMINATED", "DEPLOY_FAILED"}

class StriimError(Exception):
    pass


class StriimTimeout(StriimError):
    """A state-changing call timed out and may still be running on the server: never retried,
    and never an expected outcome."""


def _other_builds(names, jar_name: str, tag: str) -> tuple[list[str], list[str]]:
    """(content-named, plain) listed jars that are other builds of the OP in the content-named
    `jar_name`: the same name with another tag, and the plain pre-content name. Exact, so another
    OP whose name merely starts the same is never matched."""
    from livetest import opartifacts
    pattern = opartifacts.any_tag_pattern(jar_name, tag)
    if pattern is None:
        return [], []
    want, plain = jar_name.lower(), opartifacts.plain_name(jar_name, tag).lower()
    tagged = [n for n in names if n.lower() != want and pattern.fullmatch(n.lower())]
    plains = [n for n in names if n.lower() == plain and plain != want]
    return tagged, plains


def _with_refusals(e: StriimError, refused) -> StriimError:
    notes = "; ".join(r for r in refused if r)
    return StriimError(f"{e}" + (f" (UNLOAD refusals: {notes})" if notes else ""))


# Read-only calls (status, MON, DESCRIBE, SHOW ... CHECKPOINT HISTORY, LIST LIBRARIES, LIST
# DEPLOYMENTGROUPS) and the login can be abandoned safely, so they get a
# shorter timeout than striim_api's long default for state-changing commands: a poll loop with
# its own budget then overruns it by at most one call. Long enough for a slow healthy cluster.
POLL_TIMEOUT = (10, 120)

# teardown()'s graceful steps (stop, undeploy) are not started once this many seconds have gone;
# the FORCE drops always run. A call is never cut off mid-flight, because the next step would
# then run over a command the server is still executing.
TEARDOWN_BUDGET = 60.0   # inside plugin.INTERRUPT_TEARDOWN_TIMEOUT (90 s), so the drops still run


def _read_timed_out(e: Exception) -> bool:
    """striim_api.read_timed_out, looked up per call rather than at import, so a test that
    stands in a bare striim_api module can still import this one."""
    return striim_api.read_timed_out(e)


def _is_already_loaded(message: str) -> bool:
    """True when a failed LOAD means "the server already has something registered under this
    name" rather than a real error.

    Striim's wording is `The file :<jar> has already been loaded. Unload it and load again.`
    (StriimClassLoader.addJar, when the OP's PropertyTemplate is already in the MDR). Matched
    on the stable middle of that sentence: the prefix carries the jar name and the suffix is
    advice, so neither is worth pinning.
    """
    return "already been loaded" in (message or "").lower()

def probe_reachable_detail(url: str, username: str, password: str, timeout: float = 5.0) -> tuple[bool, str]:
    """Like ``probe_reachable`` but also returns *why* it failed — a bare bool collapses
    "connection refused", "timed out", "wrong credentials (401)", and "200 but no token" into the
    same False, which makes a stuck/skipped live run impossible to diagnose from its message
    alone. Callers that only need the bool can keep using ``probe_reachable``."""
    try:
        resp = requests.post(
            url.rstrip("/") + "/security/authenticate",
            data={"username": username, "password": password},
            timeout=timeout,
        )
    except requests.ConnectionError as e:
        return False, f"connection error: {e}"
    except requests.Timeout:
        return False, f"timed out after {timeout}s"
    except requests.RequestException as e:
        return False, f"request failed: {e}"
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:200]!r}"
    try:
        token = resp.json().get("token")
    except ValueError:
        return False, f"200 with unparseable body: {resp.text[:200]!r}"
    if not token:
        return False, f"200 with no token in response: {resp.text[:200]!r}"
    return True, "ok"


def probe_reachable(url: str, username: str, password: str, timeout: float = 5.0) -> bool:
    ok, _detail = probe_reachable_detail(url, username, password, timeout)
    return ok

def redact(text: str) -> str:
    """``text`` with every known secret and URL userinfo masked (livetest.evidence's rules). Output
    that may quote a rendered TQL goes through this: the TQL carries service credentials."""
    from livetest import evidence
    return evidence.redact_text(text, evidence.known_secrets(), home="", hostname="")


class StriimClient:
    def __init__(self, api):
        self.api = api

    @classmethod
    def from_url(cls, url: str, username: str, password: str) -> "StriimClient":
        parsed = urlparse(url if "://" in url else "http://" + url)
        host = parsed.hostname or "localhost"
        port = parsed.port or 9080
        # striim_api reads STRIIM_API_TIMEOUT from the environment; fill it from .env here, as
        # pytest_configure does, so preflight and ownership cleanup honour it too.
        timeout = paths.setting("STRIIM_API_TIMEOUT")
        if timeout is not None and not os.environ.get("STRIIM_API_TIMEOUT", "").strip():
            os.environ["STRIIM_API_TIMEOUT"] = timeout
        return cls(striim_api.StriimApi(host, port, username, password, login_timeout=POLL_TIMEOUT))

    def deploy_tql(self, tql_text: str) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".tql", delete=False) as f:
            f.write(tql_text)
            path = f.name
        # The API wrapper prints every per-statement answer, and an answer quotes its statement,
        # credentials included: print it masked.
        printed = io.StringIO()
        try:
            with contextlib.redirect_stdout(printed):
                resp = self.api.post_tungsten_file(path)
        except Exception as e:
            if not _read_timed_out(e):
                raise
            # Not retried: the server may still be executing the TQL, and a second import
            # would run over it.
            raise StriimTimeout(
                f"TQL import timed out ({e}); the server may still be executing it. Not retried. "
                f"STRIIM_API_TIMEOUT sets the timeout.") from e
        finally:
            Path(path).unlink(missing_ok=True)
            if printed.getvalue():
                sys.stdout.write(redact(printed.getvalue()))
        if isinstance(resp, list):
            failure = next((s for s in resp if s.get("executionStatus") == "Failure"), None)
            if failure is not None:
                # The single failing command's message alone loses everything else the
                # import ran (prior statements, later ones Striim still attempted) — dump
                # the full per-statement response so a failed import is debuggable from
                # the test's own log output, not just the one-line summary below.
                print(redact(f"[slt] TQL import failed — full response:\n{json.dumps(resp, indent=2)}"))
                raise StriimError(redact(
                    f"{failure.get('command')!r} failed: {failure.get('failureMessage')}"))

    def mon(self, name: str) -> dict:
        """`MON <name>;` -- an app's component tree, or one component's monitor figures --
        as the console reads them. The endpoint answers a list of command results; the
        figures are the first result's `output`."""
        # striim_api prints a status line per call; a monitor poll every 2s would bury the
        # one-line failure under hundreds of MON payloads.
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                resp = self.api.post_tungsten_line(f"MON {name};", timeout=POLL_TIMEOUT)
        except Exception as e:
            raise StriimError(f"could not read MON {name}: {e!r}") from e
        if isinstance(resp, list) and resp:
            first = resp[0]
            if isinstance(first, dict) and first.get("executionStatus") == "Failure":
                raise StriimError(f"MON {name} failed: {first.get('failureMessage')}")
            out = first.get("output") if isinstance(first, dict) else None
            return out if isinstance(out, dict) else {}
        return resp if isinstance(resp, dict) else {}

    def describe(self, name: str) -> list:
        """`DESCRIBE <name>;` -- the component's definition and, for a source in an app with
        RECOVERY, its `Checkpoint` entries (`Source Restart Position` and the like). Returns the
        first result's `output` as a list (one object per described entity)."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                resp = self.api.post_tungsten_line(f"DESCRIBE {name};", timeout=POLL_TIMEOUT)
        except Exception as e:
            raise StriimError(f"could not read DESCRIBE {name}: {e!r}") from e
        if isinstance(resp, list) and resp:
            first = resp[0]
            if isinstance(first, dict) and first.get("executionStatus") == "Failure":
                raise StriimError(f"DESCRIBE {name} failed: {first.get('failureMessage')}")
            out = first.get("output") if isinstance(first, dict) else None
            if isinstance(out, list):
                return out
            return [out] if isinstance(out, dict) else []
        return []

    def checkpoint_history(self, app: str) -> list:
        """`SHOW <app> CHECKPOINT HISTORY;` -- the platform's own recovery-checkpoint record
        for the app, over the same Tungsten endpoint MON reads (Grammar.cup: `SHOW name
        CHECKPOINT HISTORY`; API path: APICheckpointHistoryCommandExecutor).

        Normalizes two distinct platform shapes to one Python list: a row per recorded
        checkpoint when at least one exists, or a `CommandResponse(404, "... not available
        yet")` failure when none does. Callers only need "some rows" vs "no rows" -- treating
        the failure as `[]` rather than raising means "nothing recorded" and "the reader has
        no RECOVERY clause at all" both read the same way, which is what they are."""
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                resp = self.api.post_tungsten_line(f"SHOW {app} CHECKPOINT HISTORY;",
                                                   timeout=POLL_TIMEOUT)
        except Exception as e:
            raise StriimError(f"could not read SHOW {app} CHECKPOINT HISTORY: {e!r}") from e
        if isinstance(resp, list) and resp:
            first = resp[0]
            if isinstance(first, dict) and first.get("executionStatus") == "Failure":
                return []
            out = first.get("output") if isinstance(first, dict) else None
            return out if isinstance(out, list) else []
        return []

    def current_status(self, app: str) -> str:
        try:
            return self.api.status_application(app, timeout=POLL_TIMEOUT)
        except StriimError:
            raise
        except Exception as e:
            raise StriimError(f"could not read status for {app}: {e!r}") from e

    def await_running(self, app: str, timeout: int, poll: float = 2.0, progress=None) -> None:
        # `progress`, if given, is called each poll as progress(elapsed_s, timeout_s, last_status)
        # — a live "still waiting" heartbeat for the console (see plugin.py). Best-effort: a
        # reporting hiccup must never abort the readiness wait.
        started = time.monotonic()
        deadline = started + timeout
        last = None
        while True:
            last = self.current_status(app)
            if last == "RUNNING":
                return
            if last in TERMINAL_STATUSES:
                raise StriimError(f"{app} reached terminal status {last}")
            if time.monotonic() >= deadline:
                raise StriimError(f"{app} did not reach RUNNING within {timeout}s (last={last})")
            if progress:
                try:
                    progress(time.monotonic() - started, float(timeout), last)
                except Exception:
                    pass
            if poll:
                time.sleep(poll)

    # --- App control for the recovery phase (livetest/recovery.py) -------------------
    # Raising, unlike teardown()'s best-effort steps below: a recovery test whose interruption
    # silently failed becomes an ordinary undisturbed run that passes and proves nothing.
    #
    # BUT THESE CANNOT CARRY THAT GUARANTEE ALONE, and it would be dishonest to claim they do.
    # striim_api.stop_application/start_application never call raise_for_status() and catch only
    # HTTPError, so a 4xx/5xx returns normally with nothing raised. What they raise on is a
    # transport-level failure. Proof that the command actually TOOK comes from
    # recovery.await_left_running(), which blocks until the app has left RUNNING and raises if
    # it never does. quiesce_app goes through TQL and so can, and does, check the response.

    def stop_app(self, app: str) -> None:
        """STOP APPLICATION. NOT a drain -- Flow.stopImpl() calls stopDataFlow()+stop() and
        never flush(), so a DatabaseWriter discards its pending batch and rolls back its open
        transaction. That is the point of exposing it."""
        try:
            self.api.stop_application(app)
        except Exception as e:
            raise StriimError(f"STOP APPLICATION {app} failed: {e!r}") from e

    def start_app(self, app: str) -> None:
        try:
            self.api.start_application(app)
        except Exception as e:
            raise StriimError(f"START APPLICATION {app} failed: {e!r}") from e

    def quiesce_app(self, app: str) -> None:
        """QUIESCE APPLICATION -- the drain. Flow.quiesceFlush() injects a FlushCommandEvent,
        waits for NODE_APP_QUIESCE_FLUSHED from every deployed component, then checkpoints.

        There is no quiesce_application() on striim_api, so this goes through TQL -- which means
        the per-statement response IS available, and is inspected here exactly as deploy_tql
        does it. A rejected QUIESCE returns HTTP 200 with executionStatus "Failure"; without
        this check it would be invisible and the phase would proceed against a still-running
        app."""
        try:
            resp = self.api.post_tungsten_line(f"QUIESCE APPLICATION {app};")
        except Exception as e:
            raise StriimError(f"QUIESCE APPLICATION {app} failed: {e!r}") from e
        if isinstance(resp, list):
            failure = next((r for r in resp if r.get("executionStatus") == "Failure"), None)
            if failure is not None:
                raise StriimError(
                    f"QUIESCE APPLICATION {app} rejected: {failure.get('failureMessage')}")

    def teardown(self, app: str, namespace: str | None = None) -> None:
        # Best-effort, never raises. stop + undeploy return the app to CREATED, then
        # DROP APPLICATION ... FORCE removes it regardless of state — including when a
        # target adapter can't connect (e.g. a HALTed Spanner writer), where DROP ...
        # CASCADE fails on the adapter and leaves the namespace behind. DROP NAMESPACE
        # CASCADE then clears the (now app-less) namespace so re-runs don't collide on
        # CREATE NAMESPACE.
        #
        # Stop and undeploy are skipped once TEARDOWN_BUDGET has gone; the FORCE drops always
        # run, since they work in any state and skipping them would leave the namespace behind.
        started = time.monotonic()
        for graceful in (lambda: self.api.stop_application(app),
                         lambda: self.api.undeploy_application(app)):
            if time.monotonic() - started >= TEARDOWN_BUDGET:
                break
            try:
                graceful()
            except Exception:
                pass
        steps = [lambda: self._force_drop(app)]
        if namespace:
            steps.append(
                lambda: self.api.post_tungsten_line(
                    f"DROP NAMESPACE {namespace} CASCADE FORCE;")
            )
        for step in steps:
            try:
                step()
            except Exception:
                pass

    def teardown_namespace(self, namespace: str) -> None:
        # Multi-app variant of teardown(): a test can create more than one app in its
        # namespace (e.g. "${APP}_producer" + "${APP}_reader"). teardown(app, ...) only
        # knows about ONE app, so any sibling app is left RUNNING and blocks
        # `DROP NAMESPACE ... CASCADE` -> the next run's CREATE NAMESPACE fails with
        # "already exists". Enumerate every app in the namespace via LIST APPLICATIONS
        # (Striim has no "LIST APPLICATIONS IN <ns>" — confirmed against a live cluster:
        # that syntax 400s), stop+undeploy each (independently best-effort -- a HALTed
        # app's stop/undeploy can itself fail; that must not block the others or the
        # final DROP), force-drop each, then DROP NAMESPACE CASCADE. Never raises. Zero
        # matches (a clean namespace, or one where a prior app was already dropped) still
        # issues the DROP so the no-app case is covered too.
        #
        # DROP APPLICATION ... FORCE is the step that makes this survive a WEDGED app, and
        # it is not optional. A target adapter stuck inside a retry loop (measured: a
        # SpannerWriter parked in gax DirectRetryingExecutor.sleep under
        # runTxWithRetriesOnAborted for 55 minutes) never observes the stop signal, so STOP
        # leaves the app in STOPPING forever and UNDEPLOY then refuses with "because
        # application is STOPPING". Without the force drop the app survives teardown, holds
        # its namespace, and the next run's CREATE NAMESPACE collides. FORCE removes it
        # regardless of state. Retried once after a short pause because the drop can itself
        # land while the app is still transitioning into STOPPING.
        #
        # As in teardown(): once TEARDOWN_BUDGET has gone, the remaining apps skip stop and
        # undeploy and go straight to the FORCE drop.
        apps: list[str] = []
        try:
            # The default timeout, not POLL_TIMEOUT: a timed-out listing would skip every
            # per-app FORCE drop, which is what removes a wedged app.
            resp = self.api.post_tungsten_line("LIST APPLICATIONS;")
            apps = self._apps_in_namespace(resp, namespace)
        except Exception:
            pass
        started = time.monotonic()   # after the listing, which keeps the long default
        for app in apps:
            for graceful in (self.api.stop_application, self.api.undeploy_application):
                if time.monotonic() - started >= TEARDOWN_BUDGET:
                    break
                try:
                    graceful(app)
                except Exception:
                    pass
            self._force_drop(app)
        try:
            self.api.post_tungsten_line(f"DROP NAMESPACE {namespace} CASCADE FORCE;")
        except Exception:
            pass

    def _force_drop(self, app: str, attempts: int = 2, pause: float = 3.0) -> bool | None:
        """DROP APPLICATION <app> FORCE, best-effort. Returns True once the app is gone, False
        when it is not, and None when a DROP timed out and may still be running on the server.

        "does not exist" counts as success -- teardown is idempotent and a concurrent
        teardown (or a manual drop) having got there first is a fine outcome, not an error.
        """
        for i in range(attempts):
            try:
                resp = self.api.post_tungsten_line(f"DROP APPLICATION {app} FORCE;")
                entries = resp if isinstance(resp, list) else [resp]
                for e in entries:
                    if not isinstance(e, dict):
                        continue
                    if e.get("executionStatus") == "Success":
                        return True
                    if "does not exist" in (e.get("failureMessage") or ""):
                        return True
            except Exception as e:
                if _read_timed_out(e):
                    # The server may still be running this DROP: a second would overlap it. (A
                    # ConnectTimeout never reached the server, so it is retried like other errors.)
                    return None
            if i + 1 < attempts:
                time.sleep(pause)
        return False

    @staticmethod
    def _apps_in_namespace(resp, namespace: str) -> list[str]:
        # Extract fully-qualified app names beginning with "<namespace>." from a LIST
        # APPLICATIONS response, case-insensitively (Striim upper-cases namespaces, but
        # compare loosely rather than assume that). Observed live shape: a one-element
        # list of {"command", "executionStatus", "output": [...], "responseCode"}, where
        # each `output` item is like {"application1": {"name": "NS.App"}}. Handled
        # defensively beyond that in case of shape drift: a bare string item, an item
        # exposing "name" directly, or a wrapper dict whose value carries "name".
        prefix = (namespace + ".").lower()
        names = []
        if not isinstance(resp, list):
            return names
        for entry in resp:
            if not isinstance(entry, dict):
                continue
            output = entry.get("output")
            if not isinstance(output, list):
                continue
            for item in output:
                cand = None
                if isinstance(item, str):
                    cand = item
                elif isinstance(item, dict):
                    if isinstance(item.get("name"), str):
                        cand = item["name"]
                    else:
                        for v in item.values():
                            if isinstance(v, dict) and isinstance(v.get("name"), str):
                                cand = v["name"]
                                break
                if cand and cand.lower().startswith(prefix):
                    names.append(cand)
        return names

    def load_jar(self, name: str) -> None:
        # Globally load a UDF jar from UploadedFiles so an app's FQN function calls
        # resolve at deploy. UNLOAD first (ignored if not loaded) so re-runs re-load a
        # freshly-uploaded jar rather than hitting "already loaded". (OP jars don't use
        # this — they LOAD OPEN PROCESSOR inside the app.tql.)
        # No client timeout, as for OP jars: an abandoned UNLOAD would overlap the LOAD.
        path = f"UploadedFiles/{name}"
        try:
            self.api.post_tungsten_line(f"UNLOAD '{path}';", timeout=striim_api.NO_TIMEOUT)
        except Exception:
            pass
        resp = self.api.post_tungsten_line(f"LOAD '{path}';", timeout=striim_api.NO_TIMEOUT)
        if isinstance(resp, list) and resp and resp[-1].get("executionStatus") == "Failure":
            raise StriimError(f"LOAD '{path}' failed: {resp[-1].get('failureMessage')}")

    def load_open_processor(self, jar_name: str) -> None:
        # Register an OP jar globally via LOAD OPEN PROCESSOR (the replacement for the
        # in-TQL LOAD statement stripped by Recipe L, spec §B.2). Distinct from load_jar
        # (which LOADs a UDF jar); OP jars use LOAD OPEN PROCESSOR.
        #
        # RAW: it fails when something is already registered under this OP's name. Prefer
        # load_open_processor_idempotent, which handles that case without unloading first.
        #
        # A note on the two failure messages, because comments here used to conflate them.
        # An already-registered OP gives `The file :<jar> has already been loaded. Unload it
        # and load again.` (StriimClassLoader.addJar, on finding the PropertyTemplate in the
        # MDR -- confirmed live). `File copying failed during dependency verification` is a
        # DIFFERENT failure from Compiler.isJarAllowed, whose Files.copy into
        # .striim/OpenProcessor has no REPLACE_EXISTING and so trips over a leftover temp
        # copy from an interrupted load. Only the first means "collision".
        #
        # No client timeout: a LOAD the client abandons keeps running on the server, and the
        # UNLOAD + LOAD that would follow it is the overlap that poisons the jar's loader.
        path = f"UploadedFiles/{jar_name}"
        resp = self.api.post_tungsten_line(f'LOAD OPEN PROCESSOR "{path}";',
                                           timeout=striim_api.NO_TIMEOUT)
        if isinstance(resp, list) and resp and resp[-1].get("executionStatus") == "Failure":
            raise StriimError(
                f'LOAD OPEN PROCESSOR "{path}" failed: {resp[-1].get("failureMessage")}')

    def load_open_processor_idempotent(self, jar_name: str, tag: str | None = None,
                                       before_unload=None) -> str:
        """Make sure the OP jar is loaded, UNLOADing first only if the server says it must.

        Returns "loaded" (nothing was registered under that name) or "reloaded" (something
        was, so it had to be replaced).

        LOAD first: when nothing is registered under the OP -- every fresh cluster -- the LOAD
        simply succeeds and nothing is destroyed. Only Striim's own "has already been loaded"
        answer justifies an UNLOAD. Any OTHER failure propagates untouched: a "ZipFile invalid
        LOC header" is not a collision.

        Striim's UNLOAD rewrites its own copy of a jar in place from UploadedFiles/<name> -- even
        when it then fails because nothing is registered under that jar -- under a jar: URL
        handle it never closes. Other bytes there poison every later LOAD of the name ("ZipFile
        invalid LOC header") or make Striim keep the OLD main class beside the new jar's other
        classes, across a restart (both reproduced on 5.4.0.6C). So `before_unload(name)` runs
        before EVERY UNLOAD and must put the loaded bytes back
        (opartifacts.restore_loaded_copy).

        On a collision the OP's other builds (`tag` set: the same name with another tag, and the
        plain name) are UNLOADed; a refusal or a transport error is noted, not fatal (a stale
        listing whose registration is already gone refuses routinely), and the LOAD is tried.
        If it still collides -- `jar_name` itself holds it, the holder could not be identified,
        or the listing failed -- `jar_name` itself is UNLOADed and LOADed: UNLOAD removes the
        OP's registration whichever jar holds it. If that LOAD fails too, the error names every
        UNLOAD refusal.
        """
        try:
            self.load_open_processor(jar_name)
            return "loaded"
        except StriimError as e:
            if not _is_already_loaded(str(e)):
                raise
        # The OP's other builds first (a listed own name may be a stale listing while another
        # build holds the registration), then the name itself if the LOAD still collides.
        listed = self._list_twice() if tag else []
        candidates = sum(_other_builds(listed, jar_name, tag), []) if tag else []
        refused = [self._unload(n, before_unload) for n in candidates]
        unsafe = [r for r in refused if "restore failed" in r]
        if candidates:
            try:
                self.load_open_processor(jar_name)
                return "reloaded"
            except StriimError as e:
                if not _is_already_loaded(str(e)):
                    raise _with_refusals(e, refused) from e
                if unsafe:
                    # The self-UNLOAD below would free the OP from a holder whose copy could not
                    # be made consistent, which is the UNLOAD the restore guard exists to avoid.
                    raise _with_refusals(e, refused) from e
        refused.append(self._unload(jar_name, before_unload))
        try:
            self.load_open_processor(jar_name)
        except StriimError as e:
            raise _with_refusals(e, refused) from e
        return "reloaded"

    def _list_twice(self) -> list[str]:
        """LIST LIBRARIES with one retry (a node may still be settling); [] if it cannot answer,
        which leaves the collision to the self-reload."""
        for attempt in (0, 1):
            try:
                return self.library_file_names(timeout=POLL_TIMEOUT)
            except Exception:       # noqa: BLE001
                if not attempt:
                    time.sleep(2)
        return []

    def _unload(self, jar_name: str, before_unload=None) -> str:
        """UNLOAD `jar_name` after `before_unload`; returns Striim's refusal, or "" on success."""
        if before_unload is not None:
            try:
                before_unload(jar_name)
            except Exception as e:  # noqa: BLE001 -- unsafe to UNLOAD over other bytes: skip it
                return f"{jar_name}: not unloaded, restore failed: {e}"
        path = f"UploadedFiles/{jar_name}"
        try:
            # No client timeout, as for LOAD: an abandoned UNLOAD would overlap the next LOAD.
            resp = self.api.post_tungsten_line(f'UNLOAD OPEN PROCESSOR "{path}";',
                                               timeout=striim_api.NO_TIMEOUT)
        except Exception as e:      # noqa: BLE001 -- noted like a refusal; the LOAD decides
            return f"{jar_name}: {e!r}"
        if isinstance(resp, list) and resp and resp[-1].get("executionStatus") == "Failure":
            return f'{jar_name}: {resp[-1].get("failureMessage")}'
        return ""

    def loaded_libraries(self, timeout=None) -> set[str]:
        """The JAR FILENAMES this cluster currently has loaded, lower-cased.

        `LIST LIBRARIES` is the read-only answer to "does this cluster still have this jar?",
        and it is the right question at the right level: it is keyed on the jar filename --
        exactly what `LOAD`/`UNLOAD OPEN PROCESSOR` and `LOAD` (UDF) operate on, and exactly
        what the OP registry records -- and it covers BOTH kinds (observed live: OP jars and
        UDF jars such as ReferenceUdfV1 side by side).

        Preferred over `LIST PROPERTYTEMPLATES`, which was the first thing tried here. That
        lists OP registrations by their @PropertyTemplate NAME, which required assuming the
        template is named after the module (true for every module in this repo, but a
        convention, not a guarantee), said nothing at all about UDF jars, and mixes in ~220
        built-in adapter templates.

        Raises StriimError if the command fails, rather than returning an empty set -- an
        empty set reads as "nothing is loaded" and would trigger exactly the cluster-wide
        reload this exists to prevent. Callers decide what an unknown answer means.
        """
        return {n.lower() for n in self.library_file_names(timeout=timeout)}

    def library_file_names(self, timeout=None) -> list[str]:
        """loaded_libraries() with the server's own case, which UNLOAD needs: the path it
        names is case-sensitive on the app nodes."""
        resp = self.api.post_tungsten_line("LIST LIBRARIES;", timeout=timeout)
        if isinstance(resp, list) and resp and resp[-1].get("executionStatus") == "Failure":
            raise StriimError(f"LIST LIBRARIES failed: {resp[-1].get('failureMessage')}")
        names = []
        saw_output = False
        for entry in resp if isinstance(resp, list) else []:
            if not isinstance(entry, dict):
                continue
            output = entry.get("output")
            if not isinstance(output, list):
                # Without this guard a dict was iterated as its keys and a string as its
                # characters, so a shape drift produced a set that merely LACKED every jar --
                # every record then verifies False and every jar re-registers on every test,
                # with nothing logged. The Failure branch above does not cover it: this is a
                # Success the parser did not understand.
                continue
            saw_output = saw_output or bool(output)
            for item in output:
                # Observed shape is {"fileName": "Foo-5.4.jar"}; tolerate a bare string or a
                # nested dict in case it drifts, as _apps_in_namespace does.
                cand = None
                if isinstance(item, str):
                    cand = item
                elif isinstance(item, dict):
                    if isinstance(item.get("fileName"), str):
                        cand = item["fileName"]
                    else:
                        for v in item.values():
                            if isinstance(v, dict) and isinstance(v.get("fileName"), str):
                                cand = v["fileName"]
                                break
                if cand and cand.strip() not in names:
                    names.append(cand.strip())
        if saw_output and not names:
            raise StriimError(
                "LIST LIBRARIES returned rows this parser could not read, so the set of "
                "loaded jars is unknown. Treated as unknown rather than empty: empty reads "
                "as 'nothing is loaded' and would reload every jar on the cluster.")
        return names

    def list_deployment_groups(self) -> list:
        resp = self.api.post_tungsten_line("LIST DEPLOYMENTGROUPS;", timeout=POLL_TIMEOUT)
        if isinstance(resp, list) and resp and resp[0].get("executionStatus") == "Failure":
            raise StriimError(f"LIST DEPLOYMENTGROUPS failed: {resp[0].get('failureMessage')}")
        return resp
