import { app, BrowserWindow, ipcMain } from "electron";
import path from "path";
import isDev from "electron-is-dev";
import { setupTray } from "./tray";
import { setupIpcHandlers } from "./ipc-handlers";
import { SyncEngine } from "./sync-engine";

let mainWindow: BrowserWindow | null = null;

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

  // Hide to tray instead of closing
  mainWindow.on("close", (e) => {
    e.preventDefault();
    mainWindow?.hide();
  });
}

app.whenReady().then(() => {
  createWindow();

  const trayController = setupTray(mainWindow!);
  const syncEngine = new SyncEngine(mainWindow!);
  setupIpcHandlers(syncEngine, mainWindow!);

  // Update tray icon based on sync events
  mainWindow!.webContents.on("ipc-message", (_, channel) => {
    if (channel === "sync-start") trayController.setStatus("syncing");
    if (channel === "sync-complete") trayController.setStatus("idle");
    if (channel === "company-error") trayController.setStatus("error");
  });

  syncEngine.start();
});

// Keep running when all windows closed
app.on("window-all-closed", () => {
  // keep app running in tray
});