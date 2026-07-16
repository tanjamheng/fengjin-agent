import { app, BrowserWindow, ipcMain } from "electron";
import { join, resolve } from "path";
import { createWriteStream, existsSync, mkdirSync, statSync, renameSync, readFileSync, writeFileSync, copyFileSync } from "fs";
import type { WriteStream } from "fs";
import { createHash, randomBytes } from "crypto";
import { LauncherManager } from "./LauncherManager";

// DPI 缩放适配
app.commandLine.appendSwitch("high-dpi-support", "1");

let win: BrowserWindow | null = null;
let logStream: WriteStream | null = null;
let launcher: LauncherManager | null = null;
let wsToken = "";

// 单实例锁
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

// ── 项目根目录 ──
function getProjectRoot(): string {
  // 开发模式：__dirname = frontend/out/main，项目根 = ../../../
  // 打包模式：exe 所在目录就是项目根
  if (app.isPackaged) {
    return resolve(app.getPath("exe"), "..");
  }
  return resolve(__dirname, "../../..");
}

function createWindow(): void {
  win = new BrowserWindow({
    width: 960,
    height: 680,
    minWidth: 800,
    minHeight: 520,
    resizable: true,
    frame: false,
    titleBarStyle: "hidden",
    icon: join(__dirname,
      app.isPackaged
        ? "../renderer/assets/icon.ico"
        : "../../src/renderer/assets/icon.ico"),
    show: false, // 先隐藏，ready-to-show 再显示
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      preload: join(__dirname, "../preload/index.js"),
    },
  });

  // 日志捕获
  const logsDir = resolve(getProjectRoot(), "logs");
  if (!existsSync(logsDir)) mkdirSync(logsDir, { recursive: true });
  const logPath = join(logsDir, "renderer.log");
  if (existsSync(logPath)) {
    try {
      if (statSync(logPath).size > 5 * 1024 * 1024) {
        const bak = logPath.replace(".log", ".old.log");
        if (existsSync(bak)) renameSync(bak, logPath.replace(".log", ".2.log"));
        renameSync(logPath, bak);
      }
    } catch (e) { console.error("日志轮转失败:", e); }
  }
  try {
    logStream = createWriteStream(logPath, { flags: "a" });
    win.webContents.on("console-message", (_event, _level, message) => {
      try { logStream!.write(message + "\n"); } catch (e) { /* ignore */ }
    });
  } catch (e) { console.error("无法创建日志文件:", e); }

  // 加载页面
  if (process.env.ELECTRON_RENDERER_URL) {
    win.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    win.loadFile(join(__dirname, "../renderer/index.html"));
  }

  if (process.env.NODE_ENV !== "development") {
    win.webContents.closeDevTools();
  }

  win.once("ready-to-show", () => {
    win?.show();
    // 窗口显示后启动加载流程
    startLauncher();
  });

  win.on("closed", () => {
    win = null;
  });
}

// ── 启动器 ──

async function startLauncher(): Promise<void> {
  if (!win) return;
  const projectRoot = getProjectRoot();
  const token = getOrCreateWsToken();
  launcher = new LauncherManager(win, projectRoot, token);

  // 通知渲染进程进入加载模式
  win.webContents.send("launcher:mode", "loading");

  try {
    await launcher.start();
    // 健康检查轮询延后到 _enterSystemLoad() 中启动
    // （预处理阶段 uvicorn 未就绪，轮询无意义）
  } catch (e: any) {
    win.webContents.send("launcher:state", {
      phase: "error",
      phaseLabel: "",
      stepText: "",
      progressPercent: 0,
      showComfort: false,
      preprocessSteps: [],
      currentStepIndex: 0,
      error: e.message || "启动失败",
      showRetry: true,
      showSkip: false,
    });
  }
}

// ── IPC 处理 ──

// 渲染进程监听 launcher 状态（由 LauncherManager 主动推送）
// 这里提供渲染进程可以调用的操作

ipcMain.handle("launcher:retry", async () => {
  if (!launcher) return;
  try {
    await launcher.retry();
  } catch (e: any) {
    win?.webContents.send("launcher:state", {
      phase: "error",
      phaseLabel: "",
      stepText: "",
      progressPercent: 0,
      showComfort: false,
      preprocessSteps: [],
      currentStepIndex: 0,
      error: e.message || "重试失败",
      showRetry: true,
      showSkip: false,
    });
  }
});

ipcMain.handle("launcher:skip", async () => {
  launcher?.skipStep();
});

ipcMain.handle("launcher:completeConnect", async () => {
  launcher?.completeConnect();
});

ipcMain.handle("launcher:getState", () => {
  return launcher?.state || null;
});

ipcMain.handle("ws:getUrl", () => {
  const port = launcher?.backendPort ?? 8765;
  return `ws://127.0.0.1:${port}/ws?token=${encodeURIComponent(getOrCreateWsToken())}`;
});

ipcMain.handle("backend:isAlive", async () => {
  const port = launcher?.backendPort ?? 8765;
  const tokenHash = createHash("sha256").update(getOrCreateWsToken()).digest("hex");
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 1500);
  try {
    const res = await fetch(`http://127.0.0.1:${port}/health`, { signal: ctrl.signal });
    if (!res.ok) return false;
    const data = await res.json();
    return data?.status === "ready" && data?.token_hash === tokenHash;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
});

function getOrCreateWsToken(): string {
  if (wsToken) return wsToken;
  const dataDir = join(getProjectRoot(), "data");
  const tokenPath = join(dataDir, "ws-token");
  try {
    if (!existsSync(dataDir)) mkdirSync(dataDir, { recursive: true });
    if (existsSync(tokenPath)) {
      const existing = readFileSync(tokenPath, "utf-8").trim();
      if (/^[a-f0-9]{64}$/i.test(existing)) {
        wsToken = existing;
        return wsToken;
      }
    }
  } catch {
    // 读失败时降级为生成新 token，并继续尝试写入。
  }
  wsToken = randomBytes(32).toString("hex");
  try {
    writeFileSync(tokenPath, wsToken, { encoding: "utf-8" });
  } catch {
    // 写失败不阻塞启动；当前进程内 token 仍可用。
  }
  return wsToken;
}

function validateModelConfig(
  cfg: { api_key: string | null; base_url: string | null; model: string | null },
  label: string,
  required: boolean,
): string[] {
  const errors: string[] = [];
  const apiKey = (cfg.api_key || "").trim();
  const baseUrl = (cfg.base_url || "").trim();
  const model = (cfg.model || "").trim();
  if (required && !apiKey) errors.push(`${label} API Key 不能为空`);
  if (required && !baseUrl) errors.push(`${label} Base URL 不能为空`);
  if (baseUrl && !baseUrl.startsWith("http://") && !baseUrl.startsWith("https://")) {
    errors.push(`${label} Base URL 格式不正确（需以 http:// 或 https:// 开头）`);
  }
  if (required && !model) errors.push(`${label} 模型名不能为空`);
  return errors;
}

// 设置面板首次模式：IPC 直写 .env
ipcMain.handle("settings:writeEnv", async (_event, data: {
  main: { api_key: string | null; base_url: string | null; model: string | null };
  mind: { api_key: string | null; base_url: string | null; model: string | null };
  mind_enabled: boolean;
}) => {
  const errors = [
    ...validateModelConfig(data.main, "主模型", true),
  ];
  if (errors.length > 0) {
    return { success: false, error: errors.join("\n") };
  }

  const projectRoot = getProjectRoot();
  const envPath = join(projectRoot, ".env");
  const examplePath = join(projectRoot, ".env.example");

  // 确保 .env 存在
  if (!existsSync(envPath) && existsSync(examplePath)) {
    copyFileSync(examplePath, envPath);
  }
  if (!existsSync(envPath)) {
    return { success: false, error: ".env 文件不存在" };
  }

  // 构建更新
  const updates: Record<string, string> = {};
  const keyMap: Record<string, [string, string]> = {
    api_key: ["main", "FENGJIN_API_KEY"],
    base_url: ["main", "FENGJIN_BASE_URL"],
    model: ["main", "FENGJIN_MODEL"],
  };
  const mindKeyMap: Record<string, [string, string]> = {
    api_key: ["mind", "MIND_API_KEY"],
    base_url: ["mind", "MIND_BASE_URL"],
    model: ["mind", "MIND_MODEL"],
  };

  for (const [field, [_, envKey]] of Object.entries(keyMap)) {
    const val = data.main[field as keyof typeof data.main];
    if (val) updates[envKey] = val;
  }
  for (const [field, [_, envKey]] of Object.entries(mindKeyMap)) {
    const val = data.mind[field as keyof typeof data.mind];
    if (val) updates[envKey] = val;
  }
  if (data.mind_enabled !== undefined) {
    updates["MIND_ENABLED"] = data.mind_enabled ? "true" : "false";
  }

  // 逐行替换写入
  const content = readFileSync(envPath, "utf-8");
  const normalizedContent = content.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const lines = normalizedContent.split("\n");
  const processed = new Set<string>();
  const newLines = lines.map((line) => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) return line;
    const match = trimmed.match(/^(\w+)\s*=\s*(.*)/);
    if (match && updates[match[1]] !== undefined) {
      processed.add(match[1]);
      return `${match[1]}=${updates[match[1]]}`;
    }
    return line;
  });

  // 追加未处理的 key
  for (const [key, value] of Object.entries(updates)) {
    if (!processed.has(key)) {
      newLines.push(`${key}=${value}`);
    }
  }

  // 原子写入
  const tmpPath = envPath + ".tmp";
  writeFileSync(tmpPath, newLines.join("\r\n"), "utf-8");
  const { renameSync: mv } = require("fs");
  mv(tmpPath, envPath);

  return { success: true };
});

// 窗口控制
ipcMain.on("window-minimize", () => win?.minimize());
ipcMain.on("window-maximize", () => {
  if (win?.isMaximized()) win.unmaximize();
  else win?.maximize();
});
ipcMain.on("window-close", () => win?.close());
ipcMain.on("window-toggle-top", () => {
  if (win) win.setAlwaysOnTop(!win.isAlwaysOnTop());
});

// 打开 URL
ipcMain.handle("app:openUrl", (_event, url: string) => {
  const { shell } = require("electron");
  try {
    const parsed = new URL(url);
    if (parsed.protocol !== "https:" && parsed.protocol !== "http:") {
      return { success: false, error: "不支持的链接协议" };
    }
    shell.openExternal(url);
    return { success: true };
  } catch {
    return { success: false, error: "无效链接" };
  }
});

// GPU 崩溃
app.on("gpu-process-crashed", (_event, killed) => {
  console.error(`GPU 进程崩溃 (killed: ${killed})，建议重启应用`);
});

app.whenReady().then(() => {
  createWindow();
  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {
  app.quit();
});

app.on("before-quit", () => {
  launcher?.cleanup();
  if (logStream) {
    try { logStream.end(); } catch (e) { /* ignore */ }
    logStream = null;
  }
});
