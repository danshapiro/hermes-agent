# Plan: Fix WhatsApp Bridge Crash Loop

## Root Cause

The WhatsApp bridge process crashes due to **discarded promise rejections** from bare `startSocket()` calls, and Node.js v22 terminates the process on unhandled rejections by default.

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

### Primary: `scripts/whatsapp-bridge/bridge.js`

Three call sites need attention:

**1. Reconnection (line 169)** — needs retry on failure:

```js
// Before:
setTimeout(startSocket, reason === 515 ? 1000 : 3000);

// After:
function safeStartSocket(label) {
    startSocket().catch(err => {
        console.error(`[${label}] startSocket failed:`, err.message || err);
        // Retry after 30s — same semantics as the original code, but without crashing
        setTimeout(() => safeStartSocket(label), 30000);
    });
}
// ...
setTimeout(() => safeStartSocket('reconnect'), reason === 515 ? 1000 : 3000);
```

Note: The `connection.update` handler is re-registered inside each `startSocket()` call. When `startSocket()` rejects before `makeWASocket()` completes, `sock` was never set and no stale handlers remain. A retry from `safeStartSocket` calls `startSocket()` fresh, which re-registers everything. A lambda wrapper is needed (not `setTimeout(safeStartSocket, ...)`) because `safeStartSocket` takes a label argument.

**2. Normal startup (line 607)** — same retry pattern:

```js
// Before:
startSocket();

// After:
safeStartSocket('startup');
```

Here the HTTP server is already listening (`app.listen` callback), so a retry on failure is correct — the bridge stays responsive via HTTP and can recover the WhatsApp connection.

**3. Pair-only mode (line 596)** — log and exit:

```js
// Before:
startSocket();

// After:
startSocket().catch(err => {
    console.error('Pairing failed:', err);
    process.exit(1);
});
```

Pair-only mode is supposed to connect, save creds, and exit. If connection fails, exit with a clear error message (written to stderr → bridge.log) rather than an opaque unhandled rejection crash.

### Placement

Define `safeStartSocket` before the `connection.update` handler at line 146 (before its first use at line 169), e.g., after the `let sock = null` declaration at line 120.

### No Python-side changes needed

The Python adapter (`gateway/platforms/whatsapp.py`) correctly handles bridge exit detection via `_check_managed_bridge_exit()`. Once the bridge no longer crashes on reconnection failure, the adapter's polling loop will continue running and the bridge will retry reconnection on its own.

## Verification

1. **Static**: Confirm all three `startSocket()` call sites are guarded against unhandled rejections
2. **Node.js unit test**: Add a test in `scripts/whatsapp-bridge/allowlist.test.mjs` (or a new `bridge.test.mjs`) that verifies `startSocket` rejection handling:
   - Mock `startSocket` to reject, verify `safeStartSocket` logs the error and schedules a retry
   - Test that unhandled rejections do NOT escape to the Node.js runtime (process does not exit)
3. **Python integration test**: Add a test in `tests/gateway/test_whatsapp_connect.py` that verifies the adapter survives a bridge process exit with code 1 (current behavior already tested in `test_send_marks_retryable_fatal_when_managed_bridge_exits` — the fix changes nothing on the Python side, the test remains valid as-is)
4. **Smoke test**: Start Hermes with WhatsApp enabled, trigger a WhatsApp disconnection, confirm bridge reconnects without crashing (bridge.log shows `[reconnect] startSocket failed: ...` instead of silent process death)
