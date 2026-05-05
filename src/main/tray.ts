import { Tray, Menu, BrowserWindow, nativeImage, app } from "electron";
import path from "path";
import isDev from "electron-is-dev";

let tray: Tray | null = null;

export function setupTray(mainWindow: BrowserWindow, onSyncNow: () => void) {
  const iconPath = isDev
    ? path.join(__dirname, "../../assets/tray-icon.png")
    : path.join(process.resourcesPath, "assets", "tray-icon.png");

  try {
    const icon = nativeImage.createFromPath(iconPath);
    tray = new Tray(icon.resize({ width: 16, height: 16 }));
  } catch {
    // If icon not found in dev, create empty tray
    tray = new Tray(nativeImage.createEmpty());
  }

  tray.setToolTip("TallyBridge");

  const buildMenu = (status: "idle" | "syncing" | "error" | "paused") => {
    const statusLabel = {
      idle: "● Ready",
      syncing: "● Syncing...",
      error: "● Error — open app",
      paused: "● Paused",
    }[status];

    return Menu.buildFromTemplate([
      { label: "TallyBridge", enabled: false },
      { label: statusLabel, enabled: false },
      { type: "separator" },
      {
        label: "Open TallyBridge",
        click: () => { mainWindow.show(); mainWindow.focus(); },
      },
      {
        label: "Sync Now",
        enabled: status !== "paused",
        click: () => onSyncNow(),
      },
      { type: "separator" },
      {
        label: "Quit TallyBridge",
        click: () => { app.exit(0); },
      },
    ]);
  };

  tray.setContextMenu(buildMenu("idle"));
  tray.on("double-click", () => { mainWindow.show(); mainWindow.focus(); });

  return {
    setStatus: (status: "idle" | "syncing" | "error" | "paused") => {
      tray?.setContextMenu(buildMenu(status));
      tray?.setToolTip(`TallyBridge — ${status}`);
    },
  };
}
