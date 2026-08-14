import { app, BrowserWindow, ipcMain } from "electron";
import path from "path";
import isDev from "electron-is-dev";
import { initLogger, logger } from "./logger";
import { setupTray } from "./tray";
import { setupIpcHandlers } from "./ipc-handlers";
import { LocalPushServer } from "./local-push-server";
import { PushQueuePoller } from "./push-queue-poller";
import { SyncEngine } from "./sync-engine";
import { setupAutoUpdater } from "./updater";
import { store, resolveControlPlaneUrl, resolveControlPlaneApiKey } from "./store";
import { tallyGate } from "./tally-gate";
import { LocalStack } from "./local-stack";

if (!isDev) {
  initLogger();
}

let mainWindow: BrowserWindow | null = null;
let localPushServer: LocalPushServer | null = null;
let pushQueuePoller: PushQueuePoller | null = null;
let syncEngineRef: SyncEngine | null = null; // module-scope handle so quit handlers can reap the sync child
let localStack: LocalStack | null = null;
let stackStopped = false; // guards the re-entrant app.quit() in the before-quit handler
let isQuitting = false; // set true for a real quit (e.g. install-restart) so close-to-tray is bypassed

//***Abha
// Because we close-to-tray, the process keeps running after the window is hidden.
// Without a single-instance lock, re-launching the app (shortcut / .exe) would
// spawn a brand-new process every time, stacking up duplicate tray icons, sync
// engines, pollers and port-3002 servers. Grab the lock here: the first instance
// keeps it; any later launch fails to acquire it, so we surface the existing
// window (via the second-instance event below) and quit the duplicate.
const gotSingleInstanceLock = app.requestSingleInstanceLock();

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    // Another launch was attempted — bring the already-running window to the front.
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}
//***Abha sharma

/**
 * Point the saved config at the stack this app just started, and adopt its key.
 *
 * bootstrap.mjs mints BACKEND_API_KEY fresh on every machine, but the desktop
 * config carries its own copy of the control-plane key. Nothing keeps the two in
 * step, and when they drift every push-queue poll returns 401 for good — with a
 * message ("Unauthorized") that says nothing about two files disagreeing. That
 * cost real time once already, and it would recur on every fresh install.
 *
 * The stack is the source of truth because it is the thing that generated the
 * key, so re-derive from it on each launch rather than storing a second copy.
 *
 * Guarded on the control plane already pointing at loopback: an install still
 * aimed at the cloud keeps its own credentials untouched.
 */
function reconcileControlPlaneWithStack(stack: LocalStack) {
  const { url, apiKey } = stack.controlPlane;
  if (!url || !apiKey) return;

  const configuredUrl = resolveControlPlaneUrl({
    controlPlaneUrl: store.get("controlPlaneUrl"),
    backendUrl: store.get("backendUrl"),
  });
  const pointsAtThisMachine =
    !configuredUrl || /^https?:\/\/(127\.0\.0\.1|localhost)(:|\/|$)/i.test(configuredUrl);
  if (!pointsAtThisMachine) return;

  if (store.get("controlPlaneUrl") !== url) {
    store.set("controlPlaneUrl", url);
    logger.info(`[stack] control plane -> ${url}`);
  }

  const configuredKey = resolveControlPlaneApiKey({
    controlPlaneApiKey: store.get("controlPlaneApiKey"),
    apiKey: store.get("apiKey"),
  });
  if (configuredKey !== apiKey) {
    store.set("controlPlaneApiKey", apiKey);
    store.set("apiKey", apiKey);
    logger.info("[stack] control-plane key realigned with the local stack");
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 900,
    height: 620,
    minWidth: 720,
    minHeight: 540,
    backgroundColor: "#f8f9fa",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
    title: "TallyBridge",
    show: false, // don't show until ready
  });

  if (isDev) {
    mainWindow.loadURL("http://localhost:5173");
    mainWindow.webContents.openDevTools({ mode: "detach" });
  } else {
    mainWindow.loadFile(path.join(__dirname, "../renderer/index.html"));
  }

  // Harden the renderer: it only ever loads our own bundle, so deny opening new
  // windows and block navigation to any external URL. This limits what a
  // compromised/maliciously-injected renderer could load or where it could send
  // data (it holds backend config), without affecting normal in-app routing
  // (React Router navigates in-memory, not via real URL loads).
  mainWindow.webContents.setWindowOpenHandler(() => ({ action: "deny" as const }));
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const allowedPrefix = isDev ? "http://localhost:5173" : "file://";
    if (!url.startsWith(allowedPrefix)) {
      event.preventDefault();
    }
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow?.show();
  });

  // Hide to tray instead of closing — unless we're genuinely quitting (e.g. to install an update)
  mainWindow.on("close", (e) => {
    if (isQuitting) return;
    e.preventDefault();
    mainWindow?.hide();
  });
}

app.whenReady().then(async () => {
  //***Abha
  // A duplicate launch that failed to grab the lock has already called app.quit();
  // bail out before doing any startup work so it doesn't spin up a second engine.
  if (!gotSingleInstanceLock) return;

  // One-time migration: move existing installs off render mode (which sends the
  // whole company in one request → OOM/413 → voucher items dropped) onto hybrid,
  // which chunks vouchers straight to the Supabase ingest Edge Function. Only
  // flip clients that already have the direct-ingest endpoint configured, so
  // installs without it aren't broken. Changing the store default alone does NOT
  // touch existing saved configs — this explicit override does.
  if (!store.get("migratedToHybridV1")) {
    if (store.get("syncIngestUrl")?.trim()) {
      store.set("syncIngestMode", "hybrid");
      logger.info("[migration] syncIngestMode -> hybrid (was render)");
    }
    store.set("migratedToHybridV1", true);
  }

  // One-time migration: move existing installs to a 6-hour (360 min) sync
  // interval. Heavy 5-minute syncs hammer single-threaded TallyPrime and can
  // freeze it; 6h is the new default. Changing the store default alone does NOT
  // touch existing saved configs — this explicit override does. Runs once, so a
  // user who later picks a different interval in Settings keeps their choice.
  if (!store.get("migratedSyncInterval6hV1")) {
    store.set("syncIntervalMinutes", 360);
    store.set("migratedSyncInterval6hV1", true);
    logger.info("[migration] syncIntervalMinutes -> 360 (6h default)");
  }

  createWindow();

  // The data stack comes up before anything that talks to it: the sync engine
  // ingests through the backend, and the push server and poller both call the
  // control plane. Window first, though — bringing up Postgres takes a few
  // seconds and the user should not stare at nothing meanwhile.
  //
  // A failure here is logged and swallowed rather than fatal. The app is still
  // worth having open: Settings works, and the log says what went wrong. Killing
  // the window would leave a client with an app that vanishes on launch and no
  // way to see why.
  localStack = new LocalStack();
  try {
    await localStack.start();
    reconcileControlPlaneWithStack(localStack);
  } catch (err) {
    logger.error(`[stack] failed to start: ${err instanceof Error ? err.message : err}`);
  }

  const syncEngine = new SyncEngine(mainWindow!);
  syncEngineRef = syncEngine;
  // Let the shared Tally gate see when a sync child currently owns port 9000 so
  // no other caller fires a competing request at the single-threaded gateway.
  tallyGate.setBusyProbe(() => syncEngine.isSyncInProgress());
  localPushServer = new LocalPushServer(mainWindow!, syncEngine);
  pushQueuePoller = new PushQueuePoller(mainWindow!, syncEngine);
  localPushServer.start();
  const trayController = setupTray(
    mainWindow!,
    () => { void syncEngine.syncNow(); },
    // Graceful quit: app.quit() fires before-quit (reaps push workers + sync
    // child, flushes logs); the isQuitting flag lets the window actually close.
    () => { isQuitting = true; app.quit(); },
  );
  syncEngine.setLifecycleCallbacks({
    onSyncStart: () => trayController.setStatus("syncing"),
    onSyncComplete: (hadErrors) =>
      trayController.setStatus(hadErrors ? "error" : "idle"),
    onCompanyError: () => trayController.setStatus("error"),
    onSyncPaused: () => trayController.setStatus("paused"),
  });
  setupIpcHandlers(syncEngine, mainWindow!);

  if (syncEngine.isPaused()) {
    trayController.setStatus("paused");
  }
  // Defer the first sync so the startup voucher backfill doesn't collide with the
  // app window and TallyPrime both still warming up. TallyPrime's gateway shares a
  // single engine with its UI, so an immediate backfill freezes the user's first
  // interactions; give them a head start before background reads begin.
  setTimeout(() => syncEngine.start(), 8000);
  pushQueuePoller.start();

  if (!isDev) {
    setupAutoUpdater({
      mainWindow: mainWindow!,
      // Defer the install-restart while ANYTHING owns Tally — a sync OR an
      // in-flight push worker. tallyGate.isBusy() covers both (the old check
      // only saw syncs, so an update could interrupt a voucher write).
      isBusy: () => syncEngine.isSyncInProgress() || tallyGate.isBusy(),
      beforeInstall: () => {
        isQuitting = true;
        pushQueuePoller?.stop();
        localPushServer?.stop();
        syncEngine.kill();
      },
    });
  }
});

// Keep running when all windows closed
app.on("window-all-closed", () => {
  // keep app running in tray
});

app.on("before-quit", (event) => {
  pushQueuePoller?.stop();
  localPushServer?.stop();
  syncEngineRef?.kill();

  // Stopping Postgres is asynchronous and must finish before the process exits,
  // or the postmaster dies without a checkpoint and the next launch pays for it
  // with crash recovery. before-quit is synchronous, so hold the quit, shut the
  // stack down, then quit again — stackStopped breaks the re-entrancy, since the
  // second app.quit() fires this handler once more.
  if (localStack?.isRunning() && !stackStopped) {
    event.preventDefault();
    void localStack.stop().finally(() => {
      stackStopped = true;
      app.quit();
    });
  }
});
