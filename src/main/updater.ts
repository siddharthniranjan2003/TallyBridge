import { autoUpdater } from "electron-updater";
import { BrowserWindow, ipcMain } from "electron";
import { logger } from "./logger";

interface UpdaterOptions {
  // The window we notify so the renderer can show the update banner.
  mainWindow: BrowserWindow;
  // True while a sync run is active — we defer the restart until it's false.
  isBusy: () => boolean;
  // Called right before the install-restart so the close-to-tray handler
  // lets the quit through and background servers shut down cleanly.
  beforeInstall: () => void;
}

const IDLE_RECHECK_MS = 5 * 1000; // re-check every 5s so install lands in the first sync gap

export function setupAutoUpdater(opts: UpdaterOptions) {
  const { mainWindow } = opts;
  // Fully automatic: download as soon as an update is found, then restart to
  // install once the app is idle (see the update-downloaded handler below).
  autoUpdater.autoDownload = true;
  // Fallback only: if a downloaded update wasn't installed, apply it on next quit.
  autoUpdater.autoInstallOnAppQuit = true;

  const send = (channel: string, payload?: unknown) => {
    if (!mainWindow.isDestroyed()) {
      mainWindow.webContents.send(channel, payload);
    }
  };

  autoUpdater.on("update-available", (i) => {
    logger.info(`[updater] update available: ${i.version}`);
    send("update-available", { version: i.version });
  });
  autoUpdater.on("update-not-available", () => logger.info("[updater] up to date"));
  autoUpdater.on("download-progress", (p) => {
    send("update-progress", { percent: Math.round(p.percent) });
  });
  autoUpdater.on("error", (e) => {
    logger.error(`[updater] ${e}`);
    send("update-error", { message: e instanceof Error ? e.message : String(e) });
  });
  autoUpdater.on("update-downloaded", (i) => {
    logger.info(`[updater] downloaded ${i.version}; auto-installing when idle`);
    send("update-downloaded", { version: i.version });
    // No prompt: restart and install automatically (deferred until no sync runs).
    installWhenIdle();
  });

  function installWhenIdle() {
    if (opts.isBusy()) {
      logger.info("[updater] sync in progress — deferring install");
      setTimeout(installWhenIdle, IDLE_RECHECK_MS);
      return;
    }
    logger.info("[updater] idle — restarting to install update");
    opts.beforeInstall();
    autoUpdater.quitAndInstall(true, true); // isSilent, isForceRunAfter
  }

  // Renderer → main: client clicked "Update now".
  ipcMain.handle("download-update", () => {
    logger.info("[updater] user requested download");
    void autoUpdater.downloadUpdate();
    return { ok: true };
  });

  // Renderer → main: client clicked "Restart to install".
  ipcMain.handle("install-update", () => {
    const deferred = opts.isBusy();
    installWhenIdle();
    return { ok: true, deferred };
  });

  void autoUpdater.checkForUpdates();
  setInterval(() => void autoUpdater.checkForUpdates(), 60 * 60 * 1000); // hourly
}
