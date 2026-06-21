import type { ServerMessage } from "../../types/protocol";

/**
 * MessageParser — 解析后端 JSON 报文为类型化对象
 *
 * 职责单一：接收 raw string，返回 ServerMessage 或 null。
 * 不做业务分发——分发由 WSClient 的 onMessage 回调完成。
 */
export class MessageParser {
  /** 连续非法消息计数（连续 5 条触发 error 状态） */
  private _consecutiveErrors = 0;
  private readonly _errorThreshold = 5;

  /**
   * 解析后端 JSON 报文
   * @returns ServerMessage 或 null（非法 JSON / 缺 type 字段 / 未知 type）
   */
  parse(raw: string): ServerMessage | null {
    try {
      const obj: unknown = JSON.parse(raw);
      if (!this._isValidMessage(obj)) {
        this._consecutiveErrors++;
        return null;
      }
      this._consecutiveErrors = 0;
      return obj as ServerMessage;
    } catch {
      this._consecutiveErrors++;
      return null;
    }
  }

  /** 是否达到连续非法消息阈值 */
  get errorState(): boolean {
    return this._consecutiveErrors >= this._errorThreshold;
  }

  /** 重置连续错误计数 */
  resetErrors(): void {
    this._consecutiveErrors = 0;
  }

  private _isValidMessage(obj: unknown): obj is ServerMessage {
    return (
      typeof obj === "object" &&
      obj !== null &&
      "type" in obj &&
      typeof (obj as Record<string, unknown>).type === "string"
    );
  }
}
