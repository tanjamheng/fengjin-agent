/**
 * LauncherManager — Electron 主进程启动管理器
 *
 * 职责：环境检查 → spawn Python 后端 → 解析进度 → 健康检查
 * 所有进度通过 IPC 推给渲染进程。
 */

import { spawn, exec, ChildProcess } from "child_process";
import { join } from "path";
import { readFileSync, existsSync, copyFileSync, mkdirSync } from "fs";
import { BrowserWindow } from "electron";

// ── 类型 ──

export interface ProgressMessage {
  type: "preprocess_plan" | "progress" | "warn" | "fatal" | "ready";
  steps?: string[];
  step?: string;
  status?: string;
  error?: string;
  detail?: string;
}

export type LauncherPhase = "env_check" | "scanning" | "preprocess" | "system_load" | "done" | "error";

export interface LauncherState {
  phase: LauncherPhase;
  phaseLabel: string;        // "正在预处理..." / "正在加载系统..."
  stepText: string;          // "正在下载 AI 模型..."
  progressPercent: number;   // 0-100
  showComfort: boolean;      // 是否显示安抚文字
  preprocessSteps: string[]; // 预处理步骤清单
  currentStepIndex: number;  // 当前步骤在清单中的位置
  error: string | null;
  showRetry: boolean;
  showSkip: boolean;
  showLogs: boolean;
}

// ── 步骤名 → 用户可见文案 ──

const STEP_LABELS: Record<string, string> = {
  "model_download:bge-m3": "正在下载 AI 模型...",
  "model_quantize:bge-m3": "正在优化 AI 模型...",
  "model_download:bge-reranker-v2-m3": "正在下载 AI 模型...",
  "model_quantize:bge-reranker-v2-m3": "正在优化 AI 模型...",
  "model_download:Llama-Guard-3-1B": "正在下载安全模型...",
  "model_quantize:Llama-Guard-3-1B": "正在优化安全模型...",
};

// ── 常量 ──

const WATCHDOG_TIMEOUT_MS = 5 * 60 * 1000; // 5 分钟
const HEALTH_TIMEOUT_MS = 120_000;          // 120 秒
const HEALTH_POLL_MS = 1000;                // 每秒轮询

export class LauncherManager {
  private _win: BrowserWindow;
  private _projectRoot: string;
  private _backend: ChildProcess | null = null;
  private _state: LauncherState;
  private _watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private _healthTimer: ReturnType<typeof setTimeout> | null = null;
  private _warnTimer: ReturnType<typeof setTimeout> | null = null;
  private _healthGen: number = 0; // retry 时递增，防止旧轮询污染新后端

  // 阶段二硬编码：7 个 engine_init 步骤
  private readonly ENGINE_STEPS = [
    "engine_init:safety",
    "engine_init:memory",
    "engine_init:mood",
    "engine_init:bond",
    "engine_init:persona",
    "engine_init:rag",
    "engine_init:knowledge",
  ];

  // 阶段二步骤名映射
  private readonly ENGINE_LABELS: Record<string, string> = {
    "engine_init:safety": "正在初始化安全护栏...",
    "engine_init:memory": "正在初始化记忆系统...",
    "engine_init:mood": "正在初始化情绪引擎...",
    "engine_init:bond": "正在初始化羁绊追踪...",
    "engine_init:persona": "正在初始化角色检测...",
    "engine_init:rag": "正在加载知识库引擎...",
    "engine_init:knowledge": "正在构建知识库...",
  };

  constructor(win: BrowserWindow, projectRoot: string) {
    this._win = win;
    this._projectRoot = projectRoot;
    this._state = {
      phase: "env_check",
      phaseLabel: "",
      stepText: "",
      progressPercent: 0,
      showComfort: false,
      preprocessSteps: [],
      currentStepIndex: 0,
      error: null,
      showRetry: false,
      showSkip: false,
      showLogs: false,
    };
  }

  // ── 公共 API ──

  get state(): LauncherState {
    return this._state;
  }

  /** 完整启动流程 */
  async start(): Promise<void> {
    try {
      // 0. 窗口打开瞬间 → 立即显示"正在检查资源..." + 进度条 0%
      this._state.phase = "scanning";
      this._state.phaseLabel = "正在检查资源...";
      this._state.stepText = "";
      this._state.progressPercent = 0;
      this._emitState();

      // 1. 环境检查（静默）
      this._checkPython();
      await this._ensureVenv();
      this._checkEnvConfig();
      this._ensureDirectories();

      // 2. spawn 后端 — 后端扫描模型状态 → preprocess_plan → 切换阶段
      this._spawnBackend();
    } catch (e: any) {
      if (e.message === "NEED_CONFIG") throw e; // 向上传播给 main.ts 弹出设置面板
      this._setError(e.message || "启动失败", true, false, true);
    }
  }

  /** 重试：kill 后端 → 重新 spawn */
  async retry(): Promise<void> {
    this._clearError();
    this._killBackend();
    this._healthGen++; // 使旧轮询失效，防止误触发 _handleReady
    this._state.preprocessSteps = [];
    this._state.currentStepIndex = 0;
    try {
      this._checkPython();
      this._checkEnvConfig();
      this._spawnBackend();
    } catch (e: any) {
      if (e.message === "NEED_CONFIG") throw e; // 向上传播给 main.ts 弹出设置面板
      this._setError(e.message || "重试失败", true, false, true);
    }
  }

  /** 跳过当前步骤 */
  skipStep(): void {
    // 将当前步骤标记为完成，推进到下一步
    this._advanceProgress();
  }

  /** 清理资源 */
  cleanup(): void {
    this._clearTimers();
    this._killBackend();
  }

  // ── 环境检查 ──

  private _checkPython(): void {
    try {
      const { execSync } = require("child_process");
      const out = execSync("python --version", { encoding: "utf-8", timeout: 5000 });
      const match = out.match(/Python (\d+)\.(\d+)/);
      if (!match || parseInt(match[1]) < 3 || (parseInt(match[1]) === 3 && parseInt(match[2]) < 10)) {
        throw new Error("需要 Python 3.10+，当前: " + (out.trim() || "未检测到"));
      }
    } catch (e: any) {
      if (e.message && e.message.includes("Python")) throw e;
      throw new Error("未检测到 Python 3.10+。请前往 python.org 下载安装（安装时勾选 Add Python to PATH）");
    }
  }

  private async _ensureVenv(): Promise<void> {
    const venvPython = join(this._projectRoot, "venv", "Scripts", "python.exe");
    if (existsSync(venvPython)) return; // venv 已存在

    return new Promise((resolve, reject) => {
      const proc = spawn("python", ["-m", "venv", "venv"], {
        cwd: this._projectRoot,
      });
      const timer = setTimeout(() => {
        proc.kill();
        reject(new Error("venv 创建超时，请检查 Python 安装"));
      }, 120_000);
      proc.on("close", (code) => {
        clearTimeout(timer);
        if (code === 0) resolve();
        else reject(new Error("venv 创建失败，请检查 Python 安装"));
      });
      proc.on("error", (err) => {
        clearTimeout(timer);
        reject(new Error(`venv 创建失败: ${err.message}`));
      });
    });
  }

  private _checkEnvConfig(): void {
    const envPath = join(this._projectRoot, ".env");
    const examplePath = join(this._projectRoot, ".env.example");

    if (!existsSync(envPath)) {
      if (existsSync(examplePath)) {
        copyFileSync(examplePath, envPath);
      }
    }

    // 检查 API Key 是否为占位值
    const content = readFileSync(envPath, "utf-8");
    const hasPlaceholder = /FENGJIN_API_KEY\s*=\s*your-api-key-here/i.test(content);
    if (hasPlaceholder) {
      throw new Error("NEED_CONFIG"); // 特殊标记，由 main.ts 触发设置面板
    }
  }

  private _ensureDirectories(): void {
    const dirs = [
      join(this._projectRoot, "data", "sessions"),
      join(this._projectRoot, "data", "chroma"),
      join(this._projectRoot, "models"),
      join(this._projectRoot, "logs"),
    ];
    for (const d of dirs) {
      if (!existsSync(d)) mkdirSync(d, { recursive: true });
    }
  }

  // ── 后端进程管理 ──

  private _spawnBackend(): void {
    const venvPython = join(this._projectRoot, "venv", "Scripts", "python.exe");
    const pythonExe = existsSync(venvPython) ? venvPython : "python";

    this._backend = spawn(pythonExe, ["-m", "src.server.server"], {
      cwd: this._projectRoot,
      env: { ...process.env, FENGJIN_LAUNCHER_MODE: "1" },
      stdio: ["ignore", "pipe", "pipe"], // stdin=ignore, stdout=pipe, stderr=pipe
    });

    let stdoutBuffer = "";

    this._backend.stdout?.on("data", (chunk: Buffer) => {
      stdoutBuffer += chunk.toString("utf-8");
      const lines = stdoutBuffer.split("\n");
      stdoutBuffer = lines.pop() || ""; // 保留最后不完整行
      for (const line of lines) {
        if (line.trim()) this._handleLine(line.trim());
      }
    });

    this._backend.stderr?.on("data", (chunk: Buffer) => {
      // stderr 在 launcher 模式下为空（loguru 写文件），但以防万一
      const text = chunk.toString("utf-8").trim();
      if (text) console.error("[backend stderr]", text);
    });

    this._backend.on("error", (err) => {
      this._setError(`无法启动后端: ${err.message}`, true, false, true);
    });

    this._backend.on("close", (code) => {
      if (this._state.phase !== "done" && this._state.phase !== "error") {
        this._setError(
          `后端异常退出 (code=${code})`, true, false, true
        );
      }
    });

    this._resetWatchdog();
  }

  _killBackend(): void {
    this._clearTimers();
    if (this._backend) {
      // 移除事件监听器，防止 kill 后异步 close 事件污染 retry() 新状态
      this._backend.removeAllListeners();
      try {
        // Windows: 杀子进程树
        if (process.platform === "win32") {
          exec(`taskkill /pid ${this._backend.pid} /T /F`, () => {});
        } else {
          this._backend.kill("SIGKILL");
        }
      } catch (e) {
        // 忽略
      }
      this._backend = null;
    }
  }

  // ── 进度解析 ──

  private _handleLine(line: string): void {
    let msg: ProgressMessage;
    try {
      msg = JSON.parse(line);
    } catch {
      // 非 JSON 行 → 忽略
      return;
    }

    switch (msg.type) {
      case "preprocess_plan":
        this._handlePreprocessPlan(msg);
        break;
      case "progress":
        this._handleProgress(msg);
        break;
      case "warn":
        this._handleWarn(msg);
        break;
      case "fatal":
        this._setError(msg.detail || msg.error || "致命错误", true, false, true);
        break;
      case "ready":
        this._handleReady();
        break;
    }
  }

  private _handlePreprocessPlan(msg: ProgressMessage): void {
    const steps = msg.steps || [];
    this._state.preprocessSteps = steps;
    this._state.currentStepIndex = 0;

    if (steps.length === 0) {
      // 跳过预处理，直接进入阶段二
      this._enterSystemLoad();
      return;
    }

    // 进入阶段一：预处理
    this._state.phase = "preprocess";
    this._state.phaseLabel = "正在预处理...";
    this._state.progressPercent = 0;
    this._resetWatchdog(); // 重置看门狗，给第一个下载步骤完整的 5 分钟预算

    // 是否有模型下载步骤 → 显示安抚
    this._state.showComfort = steps.some((s) => s.startsWith("model_download"));

    const firstStep = steps[0];
    this._state.stepText = this._labelForStep(firstStep);
    this._emitState();
  }

  private _handleProgress(msg: ProgressMessage): void {
    const step = msg.step || "";
    if (msg.status === "done") {
      this._advanceProgress();
      this._resetWatchdog();
    } else {
      // start → 更新文字
      this._state.stepText = this._labelForStep(step);
      this._emitState();
    }
  }

  private _handleWarn(msg: ProgressMessage): void {
    // 非致命 → 短暂显示警告
    this._state.stepText = `⚠ ${msg.error || "步骤失败，已跳过"}`;
    this._emitState();
    // 仅 preprocess 阶段显示按钮+自动推进（下载/量化步骤可跳过）
    // system_load 阶段 warn 为纯信息提示，不显示按钮也不推进进度
    if (this._state.phase !== "preprocess") return;
    this._state.showRetry = true;
    this._state.showSkip = true;
    this._state.showLogs = true;
    this._emitState();
    if (this._warnTimer) clearTimeout(this._warnTimer);
    this._warnTimer = setTimeout(() => {
      this._warnTimer = null;
      this._state.showRetry = false;
      this._state.showSkip = false;
      this._advanceProgress();
    }, 2000);
  }

  private _handleReady(): void {
    if (this._state.phase === "done" || this._state.phase === "error") return; // 防竞态 + 防覆盖致命错误
    this._state.phase = "done";
    this._state.phaseLabel = "";
    this._state.stepText = "";
    this._state.progressPercent = 100;
    this._clearTimers();
    this._emitState();
  }

  private _advanceProgress(): void {
    if (this._state.phase === "preprocess") {
      this._state.currentStepIndex++;
      const total = this._state.preprocessSteps.length;
      if (this._state.currentStepIndex >= total) {
        // 预处理完成 → 进入阶段二
        this._enterSystemLoad();
        return;
      }
      this._state.progressPercent = Math.round((this._state.currentStepIndex / total) * 100);
      const nextStep = this._state.preprocessSteps[this._state.currentStepIndex];
      this._state.stepText = this._labelForStep(nextStep);
    } else if (this._state.phase === "system_load") {
      this._state.currentStepIndex++;
      const total = this.ENGINE_STEPS.length;
      if (this._state.currentStepIndex >= total) {
        this._state.progressPercent = 100;
      } else {
        this._state.progressPercent = Math.round((this._state.currentStepIndex / total) * 100);
        const nextStep = this.ENGINE_STEPS[this._state.currentStepIndex];
        this._state.stepText = this.ENGINE_LABELS[nextStep] || nextStep;
      }
    }
    this._emitState();
  }

  private _enterSystemLoad(): void {
    this._state.phase = "system_load";
    this._state.phaseLabel = "正在加载系统...";
    this._state.progressPercent = 0;
    this._resetWatchdog(); // 给系统加载阶段完整的看门狗预算
    this._state.currentStepIndex = 0;
    this._state.showComfort = false;
    this._state.preprocessSteps = [];
    this._state.stepText = this.ENGINE_LABELS["engine_init:safety"];
    this._emitState();
  }

  // ── 错误处理 ──

  private _setError(msg: string, retry: boolean, skip: boolean, logs: boolean): void {
    this._state.phase = "error";
    this._state.error = msg;
    this._state.showRetry = retry;
    this._state.showSkip = skip;
    this._state.showLogs = logs;
    this._clearTimers();
    this._emitState();
  }

  private _clearError(): void {
    this._state.phase = "scanning";
    this._state.phaseLabel = "正在检查资源...";
    this._state.progressPercent = 0;
    this._state.error = null;
    this._state.showRetry = false;
    this._state.showSkip = false;
    this._state.showLogs = false;
    this._emitState();
  }

  // ── 看门狗 + 健康检查 ──

  private _resetWatchdog(): void {
    if (this._watchdogTimer) clearTimeout(this._watchdogTimer);
    this._watchdogTimer = setTimeout(() => {
      this._setError("似乎卡住了，请检查网络后重试", true, false, true);
    }, WATCHDOG_TIMEOUT_MS);
  }

  startHealthPoll(): void {
    const myGen = this._healthGen;
    const startTime = Date.now();
    const poll = () => {
      if (this._healthGen !== myGen) return; // 旧代轮询，停止
      if (this._state.phase === "done" || this._state.phase === "error") return;
      if (Date.now() - startTime > HEALTH_TIMEOUT_MS) {
        this._setError("启动超时，请查看日志后重试", true, false, true);
        return;
      }
      fetch("http://127.0.0.1:8765/health")
        .then((r) => r.json())
        .then((data) => {
          if (this._healthGen !== myGen) return; // 旧代响应，丢弃
          if (data.status === "ready" && this._state.phase !== "done") {
            this._handleReady();
          } else {
            this._healthTimer = setTimeout(poll, HEALTH_POLL_MS);
          }
        })
        .catch(() => {
          if (this._healthGen !== myGen) return; // 旧代响应，丢弃
          this._healthTimer = setTimeout(poll, HEALTH_POLL_MS);
        });
    };
    poll();
  }

  private _clearTimers(): void {
    if (this._watchdogTimer) { clearTimeout(this._watchdogTimer); this._watchdogTimer = null; }
    if (this._healthTimer) { clearTimeout(this._healthTimer); this._healthTimer = null; }
    if (this._warnTimer) { clearTimeout(this._warnTimer); this._warnTimer = null; }
  }

  // ── 辅助 ──

  private _labelForStep(step: string): string {
    return STEP_LABELS[step] || this.ENGINE_LABELS[step] || step;
  }

  private _emitState(): void {
    this._win.webContents.send("launcher:state", { ...this._state });
  }

  private _sendState(
    phase: LauncherPhase, phaseLabel: string, stepText: string, percent: number
  ): void {
    this._state.phase = phase;
    this._state.phaseLabel = phaseLabel;
    this._state.stepText = stepText;
    this._state.progressPercent = percent;
    this._emitState();
  }
}
