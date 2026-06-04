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
  syncEngine.start();
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
