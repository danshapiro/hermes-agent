# Plan: Fix WhatsApp Bridge Crash Loop

## Root Cause

The WhatsApp bridge process crashes due to **discarded promise rejections** from bare async calls in `bridge.js`, and Node.js v22 terminates the process on unhandled rejections by default.

### Source Citations

**Reconnection — `scripts/whatsapp-bridge/bridge.js:169`:**
```js
setTimeout(startSocket, reason === 515 ? 1000 : 3000);
```
`startSocket` is an `async` function (declared at line 123). When called as a `setTimeout` callback, its returned promise is **discarded**. If the function rejects (e.g., `useMultiFileAuthState()` fails on corrupted session files, or `fetchLatestBaileysVersion()` fails due to network issues), the rejection is unhandled. Node.js v22 exits the process with code 1 on unhandled promise rejections.

**Same bug at two other call sites — `bridge.js:596,607`:**
```js
// Line 596 — pair-only mode
startSocket();

// Line 607 — normal startup inside app.listen callback
startSocket();
```
The initial startup calls also discard the returned promise. If `startSocket()` fails on first launch, the bridge exits silently with no error log, making the failure invisible to the Python adapter.

**Additional unhandled rejection paths in `startSocket()`:**

1. **`saveCreds()` at line 144** — the async function from `useMultiFileAuthState()` is called without `.catch()` in the `creds.update` event handler:
   ```js
   sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });
   ```
   If the filesystem fails (disk full, permissions), `saveCreds()` rejects unhandled.

2. **`messages.upsert` handler at line 182** — the `async` handler contains `await downloadMediaMessage(...)` calls (lines 263, 279, 294, 310) for image/video/audio/document processing. If Baileys' event emitter does not catch async handler rejections, any unhandled exception in this handler terminates the process:
   ```js
   sock.ev.on('messages.upsert', async ({ messages, type }) => {
       // ... await downloadMediaMessage(...) inside
   });
   ```

**Verified behavior — Node.js v22.22.0 on the live host:**
```
$ node --eval "setTimeout(async () => { throw new Error('test'); }, 0); ..."
Error: test
exit: 1
```
Unhandled rejections in `setTimeout` callbacks **do** crash the process.

**Python adapter fatality — `gateway/platforms/whatsapp.py:556-586`:**
When the bridge process exits with a non-shutdown return code, `_check_managed_bridge_exit()` treats it as a fatal error:
```python
message = f"WhatsApp bridge process exited unexpectedly (code {returncode})."
self._set_fatal_error("whatsapp_bridge_exited", message, retryable=True)
```
This fatal error propagates to the gateway, causing Hermes to exit code 1. Systemd restarts it, and the cycle repeats.

### Crash Chain

1. Bridge connects to WhatsApp → reports "connected" via `/health`
2. WhatsApp closes the connection (error 428 "Precondition Required" or 503 "Service Unavailable" — confirmed in bridge.log)
3. Bridge calls `setTimeout(startSocket, ...)` to reconnect
4. `startSocket()` fails during reconnection (network issue, corrupted state)
5. **Unhandled rejection → Node.js v22 kills bridge process** (exit code 1)
6. Python adapter's `_check_managed_bridge_exit()` detects exit → fatal error → **Hermes exits code 1**
7. systemd restarts Hermes → cycle repeats (~15 times between 12:11–13:20 PDT)

### Red Herring

The error message "Cannot connect to host 127.0.0.1:8787 ssl:default" does **not** indicate an SSL misconfiguration. It is aiohttp's default error format for **any** connection failure. Verified by testing:
- Live health check to `http://127.0.0.1:8787/health` → succeeds
- Live test to closed port `http://127.0.0.1:18787/health` → same `ssl:default` error

The `ssl:default` string simply means aiohttp used its default SSL context (none for HTTP). The real issue is the bridge process died.

### Bridge.log Evidence

```
⚠️  Connection closed (reason: 428). Reconnecting in 3s...
⚠️  Connection closed (reason: 503). Reconnecting in 3s...
```
105 bridge startups, 133 successful connections, and dozens of "Connection closed" events recorded.

## Fix

### A. `scripts/whatsapp-bridge/bridge.js` — unhandled rejection guards

**1. Add `safe-start.mjs` (new shared module):**

Extracts the retry-and-rejection-catch logic into a dependency-injectable factory, independently testable without importing Baileys:

```js
export function createSafeStart(startFn, { setTimeout, clearTimeout, consoleError } = {}) {
    let inProgress = false;
    let retryTimer = null;

    return function safeStart(label) {
        if (inProgress) return;
        if (retryTimer) {
            clearTimeout(retryTimer);
            retryTimer = null;
        }
        inProgress = true;
        startFn()
            .then(() => { inProgress = false; })
            .catch(err => {
                consoleError(`[${label}] start failed:`, err.message || err);
                inProgress = false;
                retryTimer = setTimeout(() => safeStart(label), 30000);
            });
    };
}
```

In `bridge.js`, `safeStartSocket` is created as:
```js
import { createSafeStart } from './safe-start.mjs';
const safeStartSocket = createSafeStart(startSocket);
```

This replaces an inline function with the same semantics, plus two guards not in the original:
- **`inProgress` flag**: blocks concurrent `startSocket()` calls when a reconnect fires while a previous attempt is still running
- **`retryTimer` cancellation**: when a fresh reconnect (e.g. from `connection.update` 'close') fires, any pending retry from a prior failure is cancelled — the fresh signal takes precedence

**2. Guard `saveCreds()` in `creds.update` (line 144):**

```js
// Before:
sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });

// After:
sock.ev.on('creds.update', () => {
    saveCreds().catch(err => console.error('saveCreds failed:', err));
    lidToPhone = buildLidMap();
});
```

**3. Guard `messages.upsert` handler (line 182):**

Wrap the entire handler body in try/catch so any unhandled rejection inside media downloads or message processing is caught and logged instead of terminating the process:

```js
sock.ev.on('messages.upsert', async ({ messages, type }) => {
    try {
        // ... existing handler body (lines 183-367) ...
    } catch (err) {
        console.error('messages.upsert handler error:', err);
    }
});
```

**4. Reconnection (line 169):**

```js
// Before:
setTimeout(startSocket, reason === 515 ? 1000 : 3000);

// After:
setTimeout(() => safeStartSocket('reconnect'), reason === 515 ? 1000 : 3000);
```

The initial delay respects the original 1s/3s cadence. On failure, `safeStartSocket` retries every 30s — new behavior for failure recovery (previously the process would crash). A lambda wrapper is required because `safeStartSocket` takes a label argument.

**5. Normal startup (line 607):**

```js
// Before:
startSocket();

// After:
safeStartSocket('startup');
```

**Behavioral change note:** With this fix, when the bridge starts but WhatsApp cannot authenticate, the HTTP server stays alive and the Python adapter proceeds through the warn-and-proceed path (`connect()` returns True with a warning) instead of failing hard. This is an improvement — the bridge retries in the background while the adapter remains operational. The adapter already handles this case (lines 499-536 of `whatsapp.py`).

**6. Pair-only mode (line 596):**

```js
// Before:
startSocket();

// After:
startSocket().catch(err => {
    console.error('Pairing failed:', err);
    process.exit(1);
});
```

Pair-only mode is supposed to connect, save creds, and exit. On connection failure, exit with a clear error message (written to stderr, which `hermes whatsapp` shows to the user via `subprocess.run` in `hermes_cli/main.py:1545`). No retry — user needs to retry manually.

### B. No Python-side changes required

The Python adapter (`gateway/platforms/whatsapp.py`) correctly handles bridge exit detection via `_check_managed_bridge_exit()`. Once the bridge no longer crashes on reconnection failure, the adapter's polling loop will continue running and the bridge will retry reconnection on its own.

The existing Python test `test_send_marks_retryable_fatal_when_managed_bridge_exits` in `tests/gateway/test_whatsapp_connect.py` remains valid — the adapter still correctly handles bridge process death. The fix adds resilience so the bridge dies less often, but the fatal-error path is still reachable and correctly tested.

## Verification

1. **Static**: Confirm all five unhandled-rejection paths are guarded:
   - `startSocket()` in reconnection (line 169) → `safeStartSocket`
   - `startSocket()` in pair-only (line 596) → `.catch()`
   - `startSocket()` in startup (line 607) → `safeStartSocket`
   - `saveCreds()` in creds.update (line 144) → `.catch()`
   - `messages.upsert` handler (line 182) → try/catch

2. **Node.js unit tests** (`scripts/whatsapp-bridge/bridge.test.mjs` — 6 tests, all passing):
   - Rejection catch and retry scheduling (mock setTimeout)
   - Retry on subsequent failures (timer fire simulation)
   - No unhandled rejection leak (process event listener)
   - Success path — no retry scheduled on success
   - Concurrent call prevention — `inProgress` flag blocks duplicate calls
   - Pending retry cancellation — fresh reconnect clears stale retry timer
   - Tests import `createSafeStart` from `safe-start.mjs` — the exact same module bridge.js uses, with no Baileys dependency
   - Run with: `node --test bridge.test.mjs allowlist.test.mjs` (or `npm test` in the bridge directory)

3. **CI wiring** (`.github/workflows/tests.yml`):
   - Added `Run Node tests (WhatsApp bridge)` step after Python tests
   - `package.json` `scripts.test` entry added

4. **Smoke test**: Start Hermes with WhatsApp enabled, trigger a WhatsApp disconnection, confirm bridge reconnects without crashing (bridge.log shows `[reconnect] start failed: ...` instead of silent process death).
