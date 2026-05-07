import test from 'node:test';
import assert from 'node:assert/strict';
import { createSafeStart } from './safe-start.mjs';

// Tests for the safe-start retry helper shared by bridge.js.
// Exercises the actual exported createSafeStart function via dependency
// injection (setTimeout, console.error) — no Baileys or bridge.js import
// required. This is the EXACT same function used by bridge.js's
// safeStartSocket, so coverage applies directly to production code.

test('safeStart catches rejection and schedules retry', async () => {
    const errors = [];
    const timers = [];

    const setTimeout = (fn, delay) => { timers.push({ fn, delay }); };
    const consoleError = (msg, detail) => { errors.push({ msg, detail }); };

    let callCount = 0;
    const startFn = async () => {
        callCount++;
        throw new Error('test failure');
    };

    const safeStart = createSafeStart(startFn, { setTimeout, consoleError });
    safeStart('test');

    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));

    assert.equal(callCount, 1, 'startFn should be called once');
    assert.equal(errors.length, 1, 'one error should be logged');
    assert.match(errors[0].msg, /\[test\] start failed:/);
    assert.equal(errors[0].detail, 'test failure');
    assert.equal(timers.length, 1, 'a retry should be scheduled');
    assert.equal(timers[0].delay, 30000);
});

test('safeStart retries on subsequent failures', async () => {
    const timers = [];
    const setTimeout = (fn, delay) => { timers.push({ fn, delay }); };
    const consoleError = () => {};

    let callCount = 0;
    const startFn = async () => {
        callCount++;
        throw new Error('persistent failure');
    };

    const safeStart = createSafeStart(startFn, { setTimeout, consoleError });
    safeStart('retry-test');
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
    assert.equal(timers[1].delay, 30000);
});

test('safeStart does not crash process on rejection', async () => {
    const rejectionEvents = [];
    const handler = (reason) => { rejectionEvents.push(reason); };
    process.on('unhandledRejection', handler);

    const setTimeout = () => {}; // suppress retry timer
    const consoleError = () => {};

    const startFn = async () => {
        throw new Error('should be caught');
    };

    try {
        const safeStart = createSafeStart(startFn, { setTimeout, consoleError });
        safeStart('no-crash');

        // Wait for the catch microtask to run
        await new Promise(r => globalThis.setTimeout(r, 10));

        assert.equal(rejectionEvents.length, 0, 'no unhandled rejection');
    } finally {
        process.off('unhandledRejection', handler);
    }
});

test('safeStart handles success gracefully (no retry)', async () => {
    const timers = [];
    const setTimeout = (fn, delay) => { timers.push({ fn, delay }); };
    const consoleError = () => {};

    let callCount = 0;
    const startFn = async () => {
        callCount++;
        return 'ok';
    };

    const safeStart = createSafeStart(startFn, { setTimeout, consoleError });
    safeStart('success-test');

    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));

    assert.equal(callCount, 1);
    assert.equal(timers.length, 0, 'no retry on success');
});

test('safeStart prevents concurrent calls', async () => {
    let callCount = 0;
    let resolvePromise;
    const startPromise = new Promise(r => { resolvePromise = r; });
    const startFn = async () => {
        callCount++;
        await startPromise;
    };

    const safeStart = createSafeStart(startFn);
    safeStart('first');
    safeStart('second'); // should be blocked by inProgress flag
    safeStart('third');  // should also be blocked

    resolvePromise();
    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));

    assert.equal(callCount, 1, 'only first call should execute');
});

test('safeStart cancels pending retry when fresh call arrives', async () => {
    let startFnCalls = 0;
    const cancelledTimers = [];

    const setTimeout = (fn, delay) => {
        // Return a mock timer ID that can be tracked
        const id = { fn, delay, cleared: false };
        return id;
    };
    const clearTimeout = (id) => {
        if (id) id.cleared = true;
        cancelledTimers.push(id);
    };
    const consoleError = () => {};

    // First call fails, schedules retry
    let firstCall = true;
    const startFn = async () => {
        startFnCalls++;
        if (firstCall) {
            firstCall = false;
            throw new Error('fail');
        }
    };

    const safeStart = createSafeStart(startFn, { setTimeout, clearTimeout, consoleError });

    // Call 1: fails, schedules retry
    safeStart('test');
    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));

    assert.equal(startFnCalls, 1, 'first call should execute');

    // Call 2: fresh reconnect should clear the pending retry
    firstCall = false; // second call succeeds
    safeStart('reconnect');
    await new Promise(r => process.nextTick(r));
    await new Promise(r => process.nextTick(r));

    assert.equal(startFnCalls, 2, 'second call should execute (fresh reconnect)');
    assert.ok(cancelledTimers.length >= 1, 'pending retry timer should be cancelled');
    assert.ok(cancelledTimers[0].cleared, 'timer should be cleared');
});
