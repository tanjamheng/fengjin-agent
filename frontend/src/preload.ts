import { contextBridge, ipcRenderer } from "electron";

/**
 * Preload 脚本 — 只暴露窗口控制 API
 *
 * V1 策略：只暴露 minimize / maximize / close / toggleAlwaysOnTop。
 * 渲染进程通过原生浏览器 API（WebSocket、DOM）工作，不依赖 Node 能力。
 */

contextBridge.exposeInMainWorld("electronAPI", {
  minimize: () => ipcRenderer.send("window-minimize"),
  maximize: () => ipcRenderer.send("window-maximize"),
  close: () => ipcRenderer.send("window-close"),
  toggleAlwaysOnTop: () => ipcRenderer.send("window-toggle-top"),
});
