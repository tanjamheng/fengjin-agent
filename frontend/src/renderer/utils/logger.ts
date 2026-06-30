/**
 * Logger — 前端日志薄封装
 *
 * 基于浏览器原生 console API（不引入任何库）。
 * 每条日志加时间戳 + 级别 + 模块名，输出格式与后端 app.log 对齐：
 *   [HH:MM:SS] [LEVEL] [Module] message
 *
 * 使用方式：各模块 `const log = new Logger('ModuleName')` 然后 log.info(...) 等。
 */

type LogLevel = "DEBUG" | "INFO " | "WARN " | "ERROR";

const LEVEL_MAP: Record<"debug" | "info" | "warn" | "error", LogLevel> = {
  debug: "DEBUG",
  info:  "INFO ",
  warn:  "WARN ",
  error: "ERROR",
};

/** 生成短 action_id（前端等效 trace_id，追踪一次用户操作） */
export function actionId(): string {
  return Date.now().toString(36).slice(-4) + Math.random().toString(36).slice(2, 6);
}

export class Logger {
  private _module: string;

  constructor(module: string) {
    this._module = module;
  }

  debug(msg: string, ...args: unknown[]): void {
    this._log("debug", msg, args);
  }

  info(msg: string, ...args: unknown[]): void {
    this._log("info", msg, args);
  }

  warn(msg: string, ...args: unknown[]): void {
    this._log("warn", msg, args);
  }

  error(msg: string, ...args: unknown[]): void {
    this._log("error", msg, args);
  }

  private _log(method: "debug" | "info" | "warn" | "error", msg: string, args: unknown[]): void {
    const ts = new Date().toISOString().slice(11, 19); // HH:MM:SS
    const level = LEVEL_MAP[method];
    const prefix = `[${ts}] [${level}] [${this._module}]`;
    if (args.length > 0) {
      console[method](prefix, msg, ...args);
    } else {
      console[method](prefix, msg);
    }
  }
}
