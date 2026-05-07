// Safe-start helper — wraps an async start function with rejection catching,
// re-entrancy guarding, and retry scheduling. Used by bridge.js for
// WhatsApp reconnect resilience and independently testable without
// importing Baileys or other native dependencies.

const RETRY_DELAY_MS = 30000;

/**
 * @param {() => Promise<any>} startFn  - async function to call
 * @param {object} [deps]
 * @param {Function} [deps.setTimeout]  - timeout scheduler (default: globalThis.setTimeout)
 * @param {Function} [deps.clearTimeout] - timeout clearer (default: globalThis.clearTimeout)
 * @param {Function} [deps.consoleError] - error logger (default: console.error)
 * @returns {function(string): void}  safeStart(label) — calls startFn with guards
 */
export function createSafeStart(startFn, { setTimeout = globalThis.setTimeout, clearTimeout = globalThis.clearTimeout, consoleError = console.error } = {}) {
    let inProgress = false;
    let retryTimer = null;

    return function safeStart(label) {
        if (inProgress) {
            return;
        }
        // Cancel any pending retry — a fresh reconnect (e.g. from a
        // connection.update 'close' event) takes precedence over a
        // delayed retry from a previous failure.
        if (retryTimer) {
            clearTimeout(retryTimer);
            retryTimer = null;
        }
        inProgress = true;
        startFn()
            .then(() => {
                inProgress = false;
            })
            .catch(err => {
                consoleError(`[${label}] start failed:`, err.message || err);
                inProgress = false;
                retryTimer = setTimeout(() => safeStart(label), RETRY_DELAY_MS);
            });
    };
}
