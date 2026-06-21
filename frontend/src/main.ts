import { app, BrowserWindow, ipcMain } from "electron";
import { join } from "path";

// DPI 缩放适配（在 app.whenReady() 之前）
app.commandLine.appendSwitch("high-dpi-support", "1");
app.commandLine.appendSwitch("force-device-scale-factor", "1");

let win: BrowserWindow | null = null;

// 单实例锁 — 防止多窗口 WebSocket 冲突
const gotTheLock = app.requestSingleInstanceLock();
if (!gotTheLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 960,
    height: 680,
    minWidth: 800,
    minHeight: 520,
    resizable: true,
    frame: false, // 自定义标题栏
    titleBarStyle: "hidden", // 跨平台兼容
    webPreferences: {
      // 安全策略固定值，不可修改
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: join(__dirname, "../preload/index.js"),
    },
  });

  // 开发模式加载 dev server，生产模式加载文件
  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    win.loadFile(join(__dirname, "../renderer/index.html"));
  }

  // 生产模式禁用 DevTools
  if (process.env.NODE_ENV !== "development") {
    win.webContents.closeDevTools();
  }

  win.on("closed", () => {
    win = null;
  });
}

// IPC 窗口控制
ipcMain.on("window-minimize", () => win?.minimize());
ipcMain.on("window-maximize", () => {
  if (win?.isMaximized()) {
    win.unmaximize();
  } else {
    win?.maximize();
  }
});
ipcMain.on("window-close", () => win?.close());
ipcMain.on("window-toggle-top", () => {
  if (win) {
    win.setAlwaysOnTop(!win.isAlwaysOnTop());
  }
});

// GPU 进程崩溃监听
app.on("gpu-process-crashed", (_event, killed) => {
  console.error(`GPU 进程崩溃 (killed: ${killed})，建议重启应用`);
});

app.whenReady().then(() => {
  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", () => {
  // V1: WebSocket 在渲染进程关闭时自动断开
  // V2: Three.js 资源释放在此追加
});
