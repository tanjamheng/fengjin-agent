/**
 * LauncherManager — Electron 主进程启动管理器
 *
 * 职责：环境检查 → spawn Python 后端 → 解析进度 → 健康检查
 * 所有进度通过 IPC 推给渲染进程。
 */

import { spawn, exec, ChildProcess } from "child_process";
import { join } from "path";
import { readFileSync, existsSync, copyFileSync, mkdirSync, createWriteStream, statSync, renameSync, unlinkSync, writeFileSync } from "fs";
import type { WriteStream } from "fs";
import { createHash } from "crypto";
import { BrowserWindow } from "electron";

// ── 类型 ──

export interface ProgressMessage {
  type: "preprocess_plan" | "progress" | "warn" | "fatal" | "ready";
  steps?: string[];
  step?: string;
  status?: string;
  percent?: number;  // 步骤内百分比 (0-99)，status='progress' 时有效
  error?: string;
  detail?: string;
  port?: number;
}

export type LauncherPhase = "env_check" | "scanning" | "preprocess" | "system_load" | "done" | "error";

export interface LauncherState {
  phase: LauncherPhase;
  phaseLabel: string;        // "正在预处理..." / "正在加载系统..."
  stepText: string;          // "正在下载 AI 模型..."
  progressPercent: number;   // 0-100
  stepPercent: number;       // 当前步骤内百分比 (0-100)，0=无子进度
  showComfort: boolean;      // 是否显示安抚文字
  preprocessSteps: string[]; // 预处理步骤清单
  currentStepIndex: number;  // 当前步骤在清单中的位置
  error: string | null;
  showRetry: boolean;
  showSkip: boolean;

}

// ── 步骤名 → 用户可见文案（model_* 步骤动态生成）──

const OP_LABELS: Record<string, string> = {
  download: "正在下载",
  quantize: "正在量化",
};

// ── 常量 ──

const WATCHDOG_TIMEOUT_MS = 5 * 60 * 1000; // 5 分钟 (system_load)
const PREPROCESS_WATCHDOG_MS = 15 * 60 * 1000; // 15 分钟 (preprocess，每步有进度行持续重置)
const HEALTH_POLL_MS = 2000;                // 2 秒轮询（localhost 几乎零开销）

function sha256Hex(text: string): string {
  return createHash("sha256").update(text).digest("hex");
}

export class LauncherManager {
  private _win: BrowserWindow;
  private _projectRoot: string;
  private _wsToken: string;
  private _backend: ChildProcess | null = null;
  private _state: LauncherState;
  private _watchdogTimer: ReturnType<typeof setTimeout> | null = null;
  private _healthTimer: ReturnType<typeof setTimeout> | null = null;
  private _warnTimer: ReturnType<typeof setTimeout> | null = null;
  private _healthGen: number = 0; // retry 时递增，防止旧轮询污染新后端
  private _healthPollActive = false; // 防重入
  private _logStream: WriteStream | null = null;
  private _backendStopRequested = false;
  private _backendPort = 8765;

  // 阶段二硬编码：6 个 engine_init 步骤
  private readonly ENGINE_STEPS = [
    "engine_init:safety",
    "engine_init:memory",
    "engine_init:mood",
    "engine_init:bond",
    "engine_init:persona",
    "engine_init:rag",
    "connect",
  ];

  // 阶段二步骤名映射
  private readonly ENGINE_LABELS: Record<string, string> = {
    "engine_init:safety": "正在初始化安全护栏...",
    "engine_init:memory": "正在初始化记忆系统...",
    "engine_init:mood": "正在初始化情绪引擎...",
    "engine_init:bond": "正在初始化羁绊追踪...",
    "engine_init:persona": "正在初始化角色检测...",
    "engine_init:rag": "正在加载知识库引擎...",
    "connect": "正在建立连接...",
  };

  constructor(win: BrowserWindow, projectRoot: string, wsToken: string) {
    this._win = win;
    this._projectRoot = projectRoot;
    this._wsToken = wsToken;
    this._state = {
      phase: "env_check",
      phaseLabel: "",
      stepText: "",
      progressPercent: 0,
      stepPercent: 0,
      showComfort: false,
      preprocessSteps: [],
      currentStepIndex: 0,
      error: null,
      showRetry: false,
      showSkip: false,

    };

    // 打开 launcher 专用日志（>5MB 轮转，保留一份旧日志）
    try {
      const logsDir = join(projectRoot, "logs");
      if (!existsSync(logsDir)) mkdirSync(logsDir, { recursive: true });
      const logPath = join(logsDir, "launcher.log");
      if (existsSync(logPath)) {
        try {
          if (statSync(logPath).size > 5 * 1024 * 1024) {
            const bak = logPath.replace(".log", ".old.log");
            if (existsSync(bak)) renameSync(bak, logPath.replace(".log", ".2.log"));
            renameSync(logPath, bak);
          }
        } catch { /* 轮转失败不影响功能 */ }
      }
      this._logStream = createWriteStream(logPath, { flags: "a" });
    } catch { /* 日志打不开不影响功能 */ }
  }

  private _log(message: string): void {
    const ts = new Date().toISOString();
    const line = `[${ts}] ${message}\n`;
    try { this._logStream?.write(line); } catch { /* ignore */ }
  }

  // ── 公共 API ──

  get state(): LauncherState {
    return this._state;
  }

  get backendPort(): number {
    return this._backendPort;
  }

  /** 完整启动流程 */
  async start(): Promise<void> {
    this._log("========== Launcher start ==========");
    try {
      // 0. 窗口打开瞬间 → 立即显示"正在检查资源..." + 进度条 0%
      this._state.phase = "scanning";
      this._state.phaseLabel = "正在检查资源...";
      this._state.stepText = "";
      this._state.progressPercent = 0;
      this._emitState();

      // 1. 环境检查
      this._log("Step 1: checkPython...");
      this._checkPython();
      this._log("Step 2: ensureVenv...");
      await this._ensureVenv();
      this._log("Step 3: ensurePythonDependencies...");
      await this._ensurePythonDependencies();
      this._log("Step 4: ensureEnvFile...");
      this._ensureEnvFile();
      this._log("Step 5: ensureDirectories...");
      this._ensureDirectories();

      // 2. spawn 后端 — 后端扫描模型状态 → preprocess_plan → 下载/量化 → engine_init → ready
      //    API Key 检查延后到 ready 之后（模型下载不需要 API Key，不应被拦截）
      this._log("Step 6: spawnBackend...");
      this._spawnBackend();
    } catch (e: any) {
      this._log(`FATAL: ${e.message || "启动失败"}`);
      this._setError(e.message || "启动失败", true, false);
    }
  }

  /** 重试：kill 后端 → 重新 spawn */
  async retry(): Promise<void> {
    this._log("========== Launcher retry ==========");
    this._clearError();
    this._killBackend();
    this._healthGen++; // 使旧轮询失效，防止误触发 _handleReady
    this._state.preprocessSteps = [];
    this._state.currentStepIndex = 0;
    this._state.phase = "scanning";
    this._state.phaseLabel = "正在检查资源...";
    this._state.stepText = "";
    this._state.progressPercent = 0;
    this._emitState();
    try {
      this._checkPython();
      await this._ensureVenv();
      await this._ensurePythonDependencies();
      this._spawnBackend();
    } catch (e: any) {
      this._log(`Retry FATAL: ${e.message || "重试失败"}`);
      this._setError(e.message || "重试失败", true, false);
    }
  }

  /** 跳过当前步骤 */
  skipStep(): void {
    // 将当前步骤标记为完成，推进到下一步
    this._advanceProgress();
  }

  /** 清理资源 */
  cleanup(): void {
    this._log("========== Launcher cleanup ==========");
    this._clearTimers();
    this._killBackend();
    try { this._logStream?.end(); } catch { /* ignore */ }
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
    const venvPython = this._venvPythonPath();
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

  private async _ensurePythonDependencies(): Promise<void> {
    const venvPython = this._venvPythonPath();
    const requirements = join(this._projectRoot, "requirements.txt");
    const marker = join(this._projectRoot, "venv", ".requirements-installed");
    if (!existsSync(requirements)) return;

    if (existsSync(marker)) {
      try {
        if (statSync(marker).mtimeMs >= statSync(requirements).mtimeMs) {
          await this._ensureTorchMatchesGPU(venvPython, marker);
          return;
        }
      } catch {
        // 状态异常则重新安装依赖。
      }
    }

    this._state.stepText = "正在安装 Python 依赖...";
    this._emitState();
    this._log("Installing Python dependencies from requirements.txt");

    await new Promise<void>((resolve, reject) => {
      const proc = spawn(venvPython, [
        "-m", "pip", "install", "-r", "requirements.txt",
        "-i", "https://pypi.tuna.tsinghua.edu.cn/simple",
      ], {
        cwd: this._projectRoot,
        stdio: ["ignore", "pipe", "pipe"],
      });
      const timer = setTimeout(() => {
        proc.kill();
        reject(new Error("Python 依赖安装超时，请检查网络后重试"));
      }, 20 * 60 * 1000);
      proc.stdout?.on("data", (chunk: Buffer) => {
        this._log(`[pip stdout] ${chunk.toString("utf-8").trim()}`);
      });
      proc.stderr?.on("data", (chunk: Buffer) => {
        this._log(`[pip stderr] ${chunk.toString("utf-8").trim()}`);
      });
      proc.on("close", async (code) => {
        clearTimeout(timer);
        if (code === 0) {
          await this._ensureTorchMatchesGPU(venvPython, marker);
          writeFileSync(marker, new Date().toISOString(), "utf-8");
          resolve();
        } else {
          reject(new Error(`Python 依赖安装失败 (code=${code})`));
        }
      });
      proc.on("error", (err) => {
        clearTimeout(timer);
        reject(new Error(`Python 依赖安装失败: ${err.message}`));
      });
    });
  }

  /** 验证 venv 中的 torch 是否匹配 GPU 检测结果，不匹配则重装 CUDA 版 */
  private async _ensureTorchMatchesGPU(venvPython: string, marker: string): Promise<void> {
    const gpuDetected = await new Promise<boolean>((resolve) => {
      exec("nvidia-smi", { timeout: 5000 }, (err) => resolve(!err));
    });
    if (!gpuDetected) return;

    const hasCUDA = await new Promise<boolean>((resolve) => {
      exec(`"${venvPython}" -c "import torch; print(torch.cuda.is_available())"`, (err, stdout) => {
        if (err) { resolve(false); return; }
        resolve(stdout.trim() === "True");
      });
    });
    if (hasCUDA) return;

    this._log("NVIDIA GPU detected but CPU-only PyTorch found, switching to CUDA PyTorch...");
    return new Promise<void>((resolve) => {
      const torchProc = spawn(venvPython, [
        "-m", "pip", "install", "torch==2.6.0+cu124",
        "--index-url", "https://mirrors.nju.edu.cn/pytorch/whl/cu124",
        "--extra-index-url", "https://pypi.tuna.tsinghua.edu.cn/simple",
        "--force-reinstall", "--progress-bar", "on",
      ], { cwd: this._projectRoot, stdio: ["ignore", "pipe", "pipe"] });
      torchProc.stdout?.on("data", (chunk: Buffer) => {
        this._log(`[pip cuda stdout] ${chunk.toString("utf-8").trim()}`);
      });
      torchProc.stderr?.on("data", (chunk: Buffer) => {
        this._log(`[pip cuda stderr] ${chunk.toString("utf-8").trim()}`);
      });
      torchProc.on("close", () => {
        writeFileSync(marker, new Date().toISOString(), "utf-8");
        resolve();
      });
      torchProc.on("error", (err) => {
        this._log(`CUDA torch install failed (GPU will not be used): ${err.message}`);
        writeFileSync(marker, new Date().toISOString(), "utf-8");
        resolve(); // 非致命——CPU 版也能用
      });
    });
  }

  private _venvPythonPath(): string {
    return process.platform === "win32"
      ? join(this._projectRoot, "venv", "Scripts", "python.exe")
      : join(this._projectRoot, "venv", "bin", "python");
  }

  /** 确保 .env 文件存在（首次启动从 .env.example 复制） */
  private _ensureEnvFile(): void {
    const envPath = join(this._projectRoot, ".env");
    const examplePath = join(this._projectRoot, ".env.example");

    if (!existsSync(envPath)) {
      if (existsSync(examplePath)) {
        copyFileSync(examplePath, envPath);
        this._log(".env created from .env.example");
      }
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
    const venvPython = this._venvPythonPath();
    const pythonExe = existsSync(venvPython) ? venvPython : "python";

    this._log(`Spawning backend: ${pythonExe} -m src.server.server`);
    this._backendStopRequested = false;
    this._backend = spawn(pythonExe, ["-m", "src.server.server"], {
      cwd: this._projectRoot,
      env: { ...process.env, FENGJIN_LAUNCHER_MODE: "1", FENGJIN_WS_TOKEN: this._wsToken },
      stdio: ["ignore", "pipe", "pipe"], // stdin=ignore, stdout=pipe, stderr=pipe
    });
    this._log(`Backend PID: ${this._backend.pid}`);
    this._writeBackendPid(this._backend.pid);

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
      this._setError(`无法启动后端: ${err.message}`, true, false);
    });

    const backend = this._backend;
    backend.on("close", (code, signal) => {
      const exitDetail = `code=${code ?? "null"}, signal=${signal ?? "none"}`;
      this._log(`Backend process closed (${exitDetail}, requested=${this._backendStopRequested})`);
      this._removeBackendPid(backend.pid);
      if (this._backend === backend) this._backend = null;

      // 已显示“就绪”不是允许后端静默死亡的理由。只有 launcher 明确停止时才忽略。
      if (!this._backendStopRequested) {
        this._setError(`后端已退出 (${exitDetail})，请查看 logs/uvicorn.log`, true, false);
      }
    });

    this._resetWatchdog();
  }

  _killBackend(): void {
    this._clearTimers();
    if (this._backend) {
      this._backendStopRequested = true;
      this._log(`Requesting backend stop (pid=${this._backend.pid})`);
      const backendPid = this._backend.pid;
      // 移除事件监听器，防止 kill 后异步 close 事件污染 retry() 新状态
      this._backend.removeAllListeners();
      try {
        // Windows: 杀子进程树
        if (process.platform === "win32") {
          exec(`taskkill /pid ${backendPid} /T /F`, (error) => {
            if (error) {
              this._log(`Unable to stop backend PID ${backendPid}: ${error.message}`);
              return;
            }
            this._removeBackendPid(backendPid);
          });
        } else {
          this._backend.kill("SIGKILL");
          this._removeBackendPid(backendPid);
        }
      } catch (e) {
        // 忽略
      }
      this._backend = null;
    }
  }

  /**
   * 仅记录本启动器创建的后端 PID，供 start 脚本安全清理异常遗留进程。
   * 不以端口识别进程，避免备用端口遗漏或误杀其他本地服务。
   */
  private _writeBackendPid(pid: number | undefined): void {
    if (!pid) return;
    try {
      writeFileSync(join(this._projectRoot, "logs", "backend.pid"), String(pid), "utf-8");
    } catch (e) {
      this._log(`Unable to write backend PID file: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  private _removeBackendPid(pid: number | undefined): void {
    if (!pid) return;
    const pidPath = join(this._projectRoot, "logs", "backend.pid");
    try {
      if (existsSync(pidPath) && readFileSync(pidPath, "utf-8").trim() === String(pid)) {
        unlinkSync(pidPath);
      }
    } catch (e) {
      this._log(`Unable to remove backend PID file: ${e instanceof Error ? e.message : String(e)}`);
    }
  }

  // ── 进度解析 ──

  private _handleLine(line: string): void {
    this._log(`[backend stdout] ${line}`);
    // 任何 stdout 输出都证明后端存活，重置看门狗
    this._resetWatchdog();
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
        this._setError(msg.detail || msg.error || "致命错误", true, false);
        break;
      case "ready":
        if (Number.isInteger(msg.port) && msg.port! >= 1 && msg.port! <= 65535) {
          this._backendPort = msg.port!;
          this._log(`Backend selected port: ${this._backendPort}`);
        }
        this._handleBackendInitialized();
        break;
    }
  }

  private _handlePreprocessPlan(msg: ProgressMessage): void {
    const steps = msg.steps || [];
    this._log(`preprocess_plan: ${steps.length} steps — [${steps.join(", ")}]`);
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
      this._state.stepPercent = 0;
      this._advanceProgress();
    } else if (msg.status === "progress" && msg.percent !== undefined) {
      // 步骤内子进度 → 更新百分比，用于进度条平滑过渡
      this._state.stepPercent = msg.percent;
      this._updateProgressBar();
      this._emitState();
    } else {
      // start → 更新文字
      this._state.stepText = this._labelForStep(step);
      this._state.stepPercent = 0;
      this._emitState();
    }
  }

  /** 综合 stepIndex + stepPercent 计算阶段内进度百分比 */
  private _updateProgressBar(): void {
    const idx = this._state.currentStepIndex;
    const sub = this._state.stepPercent;
    if (this._state.phase === "preprocess") {
      const total = this._state.preprocessSteps.length || 1;
      this._state.progressPercent = Math.round((idx * 100 + sub) / total);
    } else if (this._state.phase === "system_load") {
      const total = this.ENGINE_STEPS.length;
      this._state.progressPercent = Math.round((idx * 100 + sub) / total);
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
    this._log("Backend healthy — entering connect step, waiting for renderer WS");
    this._clearTimers();

    // 进入 connect 步骤（ENGINE_STEPS 最后一步），与其他步骤平分进度条
    const connectIdx = this.ENGINE_STEPS.length - 1; // 0-indexed, last step
    this._state.phase = "system_load";
    this._state.phaseLabel = "正在加载系统...";
    this._state.currentStepIndex = connectIdx;
    this._state.stepText = this.ENGINE_LABELS["connect"];
    this._state.stepPercent = 0;
    this._updateProgressBar();
    this._emitState();
    // 不发送 "done"——等待渲染进程 WS 连接完成后回调 completeConnect()
  }

  /** 渲染进程确认 WS 连接完成 → 完成 connect 步骤 → 发送 done */
  completeConnect(): void {
    if (this._state.phase === "done" || this._state.phase === "error") return;
    this._log("Renderer WS connected — completing connect step");
    this._state.phase = "done";
    this._state.phaseLabel = "";
    this._state.stepText = "";
    this._state.progressPercent = 100;
    this._emitState();

    // 后端就绪后检查 API Key，占位值则弹首次配置
    this._checkAndNotifyConfig();
  }

  /**
   * 后端初始化完成不等于端口已监听。必须等待带 token 校验的 /health 成功，
   * 才能切到聊天界面，避免端口绑定失败时出现“假就绪”。
   */
  private _handleBackendInitialized(): void {
    if (this._state.phase === "done" || this._state.phase === "error") return;
    this._log("Backend initialization complete — awaiting health check");
    // 不覆盖当前步骤（可能已由 _advanceProgress 进入 connect 步骤）
    // 只确保阶段标签正确，然后启动健康检查
    this._state.phase = "system_load";
    this._state.phaseLabel = "正在加载系统...";
    this._emitState();
    this.startHealthPoll();
  }

  /** 后端就绪后检查主模型三个字段（API Key / Base URL / 模型名）是否为空 */
  private _checkAndNotifyConfig(): void {
    try {
      const envPath = join(this._projectRoot, ".env");
      if (!existsSync(envPath)) {
        this._log(".env not found, dispatching needConfig");
        this._win.webContents.send("launcher:needConfig");
        return;
      }
      const content = readFileSync(envPath, "utf-8");
      for (const key of ["FENGJIN_API_KEY", "FENGJIN_BASE_URL", "FENGJIN_MODEL"]) {
        // [^\S\r\n] = 空格/制表符但排除换行符。\.env 可能被写为 LF，\s 会吞 \n 越界
        const m = content.match(new RegExp(`^${key}[^\\S\\r\\n]*=[^\\S\\r\\n]*(.*)$`, "m"));
        if (!m || m[1].trim() === "") {
          this._log(`Missing/empty: ${key}, dispatching needConfig`);
          this._win.webContents.send("launcher:needConfig");
          return;
        }
      }
    } catch (e) {
      this._log(`Config check error: ${e}`);
    }
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
      const nextStep = this._state.preprocessSteps[this._state.currentStepIndex];
      this._state.stepText = this._labelForStep(nextStep);
      this._state.stepPercent = 0;
      this._updateProgressBar();
    } else if (this._state.phase === "system_load") {
      this._state.currentStepIndex++;
      const total = this.ENGINE_STEPS.length;
      if (this._state.currentStepIndex >= total) {
        this._state.progressPercent = 100;
      } else {
        const nextStep = this.ENGINE_STEPS[this._state.currentStepIndex];
        this._state.stepText = this.ENGINE_LABELS[nextStep] || nextStep;
        this._state.stepPercent = 0;
        this._updateProgressBar();
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

  private _setError(msg: string, retry: boolean, skip: boolean): void {
    this._log(`ERROR: ${msg} (retry=${retry} skip=${skip})`);
    this._state.phase = "error";
    this._state.error = msg;
    this._state.showRetry = retry;
    this._state.showSkip = skip;
    this._clearTimers();
    this._emitState();
  }

  private _clearError(): void {
    this._state.phase = "scanning";
    this._state.phaseLabel = "正在检查资源...";
    this._state.progressPercent = 0;
    this._state.stepPercent = 0;
    this._state.error = null;
    this._state.showRetry = false;
    this._state.showSkip = false;
    this._healthPollActive = false; // 允许 retry 后重新启动健康轮询
    this._emitState();
  }

  // ── 看门狗 + 健康检查 ──

  private _resetWatchdog(): void {
    if (this._watchdogTimer) clearTimeout(this._watchdogTimer);
    // 预处理阶段使用宽松的 15 分钟看门狗（模型下载/知识库构建可能很慢但有进度输出）；
    // 系统加载阶段使用 5 分钟看门狗（每个步骤应在数秒内完成）
    const timeout = this._state.phase === "preprocess"
      ? PREPROCESS_WATCHDOG_MS
      : WATCHDOG_TIMEOUT_MS;
    this._watchdogTimer = setTimeout(() => {
      this._setError("似乎卡住了，请检查网络后重试", true, false);
    }, timeout);
  }

  startHealthPoll(): void {
    if (this._healthPollActive) return; // 防重入
    this._healthPollActive = true;
    const myGen = this._healthGen;
    const poll = () => {
      if (this._healthGen !== myGen) { this._healthPollActive = false; return; }
      if (this._state.phase === "done" || this._state.phase === "error") {
        this._healthPollActive = false;
        return;
      }
      fetch(`http://127.0.0.1:${this._backendPort}/health`)
        .then((r) => (r.ok ? r.json() : null))
        .then((data) => {
          if (this._healthGen !== myGen) { this._healthPollActive = false; return; }
          const expectedHash = sha256Hex(this._wsToken);
          if (data?.status === "ready" && data.token_hash === expectedHash && this._state.phase !== "done") {
            this._healthPollActive = false;
            this._handleReady();
          } else {
            this._healthTimer = setTimeout(poll, HEALTH_POLL_MS);
          }
        })
        .catch(() => {
          if (this._healthGen !== myGen) { this._healthPollActive = false; return; }
          this._healthTimer = setTimeout(poll, HEALTH_POLL_MS);
        });
    };
    poll();
  }

  private _clearTimers(): void {
    if (this._watchdogTimer) { clearTimeout(this._watchdogTimer); this._watchdogTimer = null; }
    if (this._healthTimer) { clearTimeout(this._healthTimer); this._healthTimer = null; }
    if (this._warnTimer) { clearTimeout(this._warnTimer); this._warnTimer = null; }
    this._healthPollActive = false;
  }

  // ── 辅助 ──

  /** 将 step id 转为用户可见文案。model_download:bge-m3 → "正在下载 bge-m3..." */
  private _labelForStep(step: string): string {
    // engine_init:* → 使用硬编码映射
    if (this.ENGINE_LABELS[step]) return this.ENGINE_LABELS[step];
    // knowledge_build → 预处理阶段知识库构建
    if (step === "knowledge_build") return "正在构建知识库...";
    // model_{op}:{name} → 动态生成如 "正在下载 bge-m3..."
    const m = step.match(/^model_(download|quantize):(.+)$/);
    if (m) {
      const op = OP_LABELS[m[1]] || m[1];
      return `${op} ${m[2]}...`;
    }
    return step;
  }

  private _emitState(): void {
    this._log(`emitState → phase=${this._state.phase} label="${this._state.phaseLabel}" step="${this._state.stepText}" pct=${this._state.progressPercent}% error="${this._state.error || ""}"`);
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
