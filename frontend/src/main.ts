import { app, BrowserWindow, ipcMain } from "electron";
import { join, resolve } from "path";
import { createWriteStream, existsSync, mkdirSync, statSync, renameSync } from "fs";
import type { WriteStream } from "fs";

// DPI 缩放适配（在 app.whenReady() 之前）
app.commandLine.appendSwitch("high-dpi-support", "1");
// V1 移除 device-scale-factor 强设为 1，使用系统默认缩放（HiDPI 适配）

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

  // 日志：捕获渲染进程 console 输出 → logs/renderer.log
  const logsDir = resolve(__dirname, '../../../logs');
  if (!existsSync(logsDir)) mkdirSync(logsDir, { recursive: true });
  const logPath = join(logsDir, 'renderer.log');

  // 启动时简单轮转：超过 5MB 则重命名
  if (existsSync(logPath)) {
    try {
      if (statSync(logPath).size > 5 * 1024 * 1024) {
        const bak = logPath.replace('.log', '.old.log');
        if (existsSync(bak)) renameSync(bak, logPath.replace('.log', '.2.log'));
        renameSync(logPath, bak);
      }
    } catch { /* 轮转失败不阻塞启动 */ }
  }

  let logStream: WriteStream | null = null;
  try {
    logStream = createWriteStream(logPath, { flags: 'a' });
  } catch { /* 日志文件不可用时静默降级 */ }

  if (logStream) {
    win.webContents.on('console-message', (_event, _level, message) => {
      try {
        logStream!.write(message + '\n');
      } catch { /* 写失败不阻塞渲染 */ }
    });
  }

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
