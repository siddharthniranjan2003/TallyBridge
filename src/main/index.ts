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
import { store } from "./store";

if (!isDev) {
  initLogger();
}

let mainWindow: BrowserWindow | null = null;
let localPushServer: LocalPushServer | null = null;
let pushQueuePoller: PushQueuePoller | null = null;
let isQuitting = false; // set true for a real quit (e.g. install-restart) so close-to-tray is bypassed

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
    mainWindow.loadFile(
      path.join(__dirname, "../renderer/index.html")
    );
  }

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

app.whenReady().then(() => {
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

  const syncEngine = new SyncEngine(mainWindow!);
  localPushServer = new LocalPushServer(mainWindow!);
  pushQueuePoller = new PushQueuePoller(mainWindow!, syncEngine);
  localPushServer.start();
  const trayController = setupTray(mainWindow!, () => {
    void syncEngine.syncNow();
  });
  syncEngine.setLifecycleCallbacks({
    onSyncStart: () => trayController.setStatus("syncing"),
    onSyncComplete: (hadErrors) => trayController.setStatus(hadErrors ? "error" : "idle"),
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
      isBusy: () => syncEngine.isSyncInProgress(),
      beforeInstall: () => {
        isQuitting = true;
        pushQueuePoller?.stop();
        localPushServer?.stop();
      },
    });
  }
});

// Keep running when all windows closed
app.on("window-all-closed", () => {
  // keep app running in tray
});

app.on("before-quit", () => {
  pushQueuePoller?.stop();
  localPushServer?.stop();
});
