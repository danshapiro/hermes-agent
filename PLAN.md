# Plan: Fix WhatsApp Bridge Crash Loop

## Root Cause

The WhatsApp bridge process crashes due to an **unhandled promise rejection** in its reconnection logic, and Node.js v22 terminates the process on unhandled rejections by default.

### Source Citations

**Bridge reconnection — `scripts/whatsapp-bridge/bridge.js:169`:**
```js
setTimeout(startSocket, reason === 515 ? 1000 : 3000);
```
`startSocket` is an `async` function (declared at line 123). When called as a `setTimeout` callback, its returned promise is **discarded**. If the function rejects (e.g., `useMultiFileAuthState()` fails on corrupted session files, or `fetchLatestBaileysVersion()` fails due to network issues), the rejection is unhandled. Node.js v22 exits the process with code 1 on unhandled promise rejections.

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

### Primary: `scripts/whatsapp-bridge/bridge.js:169`

Wrap the `setTimeout` callback to handle promise rejections:

```js
// Before:
setTimeout(startSocket, reason === 515 ? 1000 : 3000);

// After:
setTimeout(() => {
    startSocket().catch(err => console.error('Reconnect failed:', err));
}, reason === 515 ? 1000 : 3000);
```

This ensures that if `startSocket()` rejects during reconnection:
1. The error is logged to stderr → bridge log file
2. The Node.js process does NOT crash
3. The bridge HTTP server stays up, allowing Hermes to poll successfully

### No Python-side changes needed

The Python adapter (`gateway/platforms/whatsapp.py`) correctly handles bridge exit detection via `_check_managed_bridge_exit()`. Once the bridge no longer crashes on reconnection failure, the adapter's polling loop will continue running and the bridge will retry reconnection on its own.

## Verification

1. **Static**: Confirm `bridge.js` line 169 wraps the callback
2. **Smoke test**: Start Hermes with WhatsApp enabled, trigger a WhatsApp disconnection, confirm bridge reconnects without crashing
3. **Bridge log**: After fix, expect to see `Reconnect failed: ...` messages (from the `.catch()`) instead of silent bridge process death
