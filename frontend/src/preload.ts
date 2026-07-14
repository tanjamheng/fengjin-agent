import { contextBridge, ipcRenderer } from "electron";

/**
 * Preload 脚本 — 暴露窗口控制 + 启动器 + 设置 IPC API
 */

contextBridge.exposeInMainWorld("electronAPI", {
  // 窗口控制
  minimize: () => ipcRenderer.send("window-minimize"),
  maximize: () => ipcRenderer.send("window-maximize"),
  close: () => ipcRenderer.send("window-close"),
  toggleAlwaysOnTop: () => ipcRenderer.send("window-toggle-top"),

  // 启动器
  onLauncherState: (callback: (state: any) => void) => {
    ipcRenderer.on("launcher:state", (_event, state) => callback(state));
  },
  onLauncherMode: (callback: (mode: string) => void) => {
    ipcRenderer.on("launcher:mode", (_event, mode) => callback(mode));
  },
  onLauncherNeedConfig: (callback: () => void) => {
    ipcRenderer.on("launcher:needConfig", () => callback());
  },
  launcherRetry: () => ipcRenderer.invoke("launcher:retry"),
  launcherSkip: () => ipcRenderer.invoke("launcher:skip"),
  launcherCompleteConnect: () => ipcRenderer.invoke("launcher:completeConnect"),
  launcherGetState: () => ipcRenderer.invoke("launcher:getState"),
  getWsUrl: () => ipcRenderer.invoke("ws:getUrl"),
  isBackendAlive: () => ipcRenderer.invoke("backend:isAlive"),

  // 设置面板（首次模式：直写 .env）
  settingsWriteEnv: (data: any) => ipcRenderer.invoke("settings:writeEnv", data),

  // 工具
  openUrl: (url: string) => ipcRenderer.invoke("app:openUrl"),
});
