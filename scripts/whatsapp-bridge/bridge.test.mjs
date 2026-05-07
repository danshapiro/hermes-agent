import test from 'node:test';
import assert from 'node:assert/strict';

// safeStartSocket pattern tests
// Verifies that the rejection-catch + retry pattern in bridge.js correctly
// prevents Node.js v22 from terminating on unhandled promise rejections.

const SAFE_START_RETRY_MS = 30000;

function safeStart(label, startFn) {
    startFn().catch(err => {
        console.error(`[${label}] start failed:`, err.message || err);
        setTimeout(() => safeStart(label, startFn), SAFE_START_RETRY_MS);
    });
}

test('safeStart catches rejection and schedules retry', async () => {
    const errors = [];
    const timers = [];

    const origError = console.error;
    const origSetTimeout = globalThis.setTimeout;

    console.error = (msg, detail) => {
        errors.push({ msg, detail });
    };
    globalThis.setTimeout = (fn, delay) => {
        timers.push({ fn, delay });
        // Call the fn so the test completes (real setTimeout would wait)
        // We don't call it — we just verify it was scheduled
    };

    let callCount = 0;
    const failingStart = async () => {
        callCount++;
        throw new Error('test failure');
    };

    try {
        safeStart('test', failingStart);

        // safeStart calls startFn synchronously, then .catch runs as microtask
        await new Promise(r => process.nextTick(r));
        await new Promise(r => process.nextTick(r));

        assert.equal(callCount, 1, 'startFn should be called once');
        assert.equal(errors.length, 1, 'one error should be logged');
        assert.match(errors[0].msg, /\[test\] start failed:/);
        assert.equal(errors[0].detail, 'test failure');
        assert.equal(timers.length, 1, 'a retry should be scheduled');
        assert.equal(timers[0].delay, SAFE_START_RETRY_MS);
    } finally {
        console.error = origError;
        globalThis.setTimeout = origSetTimeout;
    }
});

test('safeStart retries on subsequent failures', async () => {
    const timers = [];
    const origSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = (fn, delay) => {
        timers.push({ fn, delay });
    };
    const origError = console.error;
    console.error = () => {};

    let callCount = 0;
    const failingStart = async () => {
        callCount++;
        throw new Error('persistent failure');
    };

    try {
        safeStart('retry-test', failingStart);
        await new Promise(r => process.nextTick(r));
        await new Promise(r => process.nextTick(r));

        assert.equal(callCount, 1);
        assert.equal(timers.length, 1);

        // Simulate retry timeout firing
        timers[0].fn();
        await new Promise(r => process.nextTick(r));
        await new Promise(r => process.nextTick(r));

        assert.equal(callCount, 2);
        assert.equal(timers.length, 2);
        assert.equal(timers[1].delay, SAFE_START_RETRY_MS);
    } finally {
        console.error = origError;
        globalThis.setTimeout = origSetTimeout;
    }
});

test('safeStart does not crash process on rejection', async () => {
    const rejectionEvents = [];
    const handler = (reason) => { rejectionEvents.push(reason); };
    process.on('unhandledRejection', handler);

    const origSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = () => {}; // suppress retry timer

    const failingStart = async () => {
        throw new Error('should be caught');
    };

    try {
        safeStart('no-crash', failingStart);

        // Wait for the catch handler microtask to run
        await new Promise(r => origSetTimeout(r, 10));

        assert.equal(rejectionEvents.length, 0, 'no unhandled rejection');
    } finally {
        process.off('unhandledRejection', handler);
        globalThis.setTimeout = origSetTimeout;
    }
});

test('safeStart handles success gracefully (no retry)', async () => {
    const timers = [];
    const origSetTimeout = globalThis.setTimeout;
    globalThis.setTimeout = (fn, delay) => {
        timers.push({ fn, delay });
    };
    const origError = console.error;
    console.error = () => {};

    let callCount = 0;
    const succeedingStart = async () => {
        callCount++;
        return 'ok';
    };

    try {
        safeStart('success-test', succeedingStart);

        // Success path: .then runs, not .catch — verify via microtask wait
        await new Promise(r => process.nextTick(r));
        await new Promise(r => process.nextTick(r));

        assert.equal(callCount, 1);
        assert.equal(timers.length, 0, 'no retry on success');
    } finally {
        console.error = origError;
        globalThis.setTimeout = origSetTimeout;
    }
});
