// TallyPrime's gateway on port 9000 is single-threaded and shares ONE calc
// engine with its on-screen UI. If two requests reach it at the same instant
// (e.g. the status-bar connectivity ping overlapping a voucher export, or a
// cloud push landing mid-sync) TallyPrime can hard-crash with a memory access
// violation (Software Exception c0000005). This gate is the single coordination
// point that guarantees TallyBridge never lets two callers touch port 9000 at
// once.
//
// Two kinds of work are tracked:
//   1. In-process HTTP from the Electron main process (axios) — serialized via
//      runExclusive() so two main-process requests never overlap.
//   2. Out-of-process Python workers (sync engine, push poller, local push
//      server) that own the gateway while they run — tracked via the busy probe
//      and the python work counter so the main process can avoid issuing a
//      competing request while a worker is active.

type BusyProbe = () => boolean;

class TallyAccessGate {
  private tail: Promise<void> = Promise.resolve();
  private activeHttp = 0;
  private pythonWork = 0;
  private busyProbe: BusyProbe | null = null;

  // Register a predicate (the sync engine's isSyncInProgress) so the gate knows
  // when a long-running Python sync child currently owns the Tally gateway.
  setBusyProbe(probe: BusyProbe) {
    this.busyProbe = probe;
  }

  // True when anything is currently talking to (or about to talk to) Tally:9000.
  isBusy(): boolean {
    if (this.activeHttp > 0 || this.pythonWork > 0) {
      return true;
    }
    try {
      return this.busyProbe ? this.busyProbe() : false;
    } catch {
      return false;
    }
  }

  // True when a push worker or a main-process HTTP request currently owns the
  // gateway. Unlike isBusy(), this EXCLUDES the sync busyProbe, so the sync
  // engine can ask "is anyone OTHER than me about to touch Tally?" before it
  // starts a run — without self-triggering on its own busyProbe. This is what
  // closes the sync-vs-push c0000005 collision (the push paths already defer to
  // an in-progress sync via isSyncInProgress; this makes the sync defer to an
  // in-flight push symmetrically).
  isExternallyBusy(): boolean {
    return this.activeHttp > 0 || this.pythonWork > 0;
  }

  // Serialize main-process HTTP to Tally. Calls queue behind one another so the
  // gateway only ever sees a single request from the Electron side at a time.
  async runExclusive<T>(fn: () => Promise<T>): Promise<T> {
    const previous = this.tail;
    let release!: () => void;
    this.tail = new Promise<void>((resolve) => {
      release = resolve;
    });
    await previous.catch(() => undefined);
    this.activeHttp += 1;
    try {
      return await fn();
    } finally {
      this.activeHttp -= 1;
      release();
    }
  }

  // Bracket an out-of-process Python worker that owns the gateway while it runs.
  beginPythonWork() {
    this.pythonWork += 1;
  }

  endPythonWork() {
    this.pythonWork = Math.max(0, this.pythonWork - 1);
  }
}

export const tallyGate = new TallyAccessGate();
