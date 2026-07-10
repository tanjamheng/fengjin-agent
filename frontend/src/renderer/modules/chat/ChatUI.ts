import { CONFIG } from "../../config";
import { MessageRenderer } from "./MessageRenderer";
import { InputController } from "./InputController";
import { show as showContextMenu } from "./ContextMenu";
import type { ChatMessage, ConnectionStatus } from "../../types/protocol";

/**
 * ChatUI — 对话区 DOM 管理
 *
 * 组合 MessageRenderer + InputController，管理整个对话区的
 * 消息渲染、滚动行为、思考中状态、连接状态、快捷回复。
 */
export class ChatUI {
  private _container: HTMLElement;
  private _renderer: MessageRenderer;
  private _input: InputController;

  private _thinkingEl: HTMLElement;
  private _quickRepliesEl: HTMLElement;
  private _statusBarEl: HTMLElement;
  private _scrollHintEl: HTMLElement;
  private _greetingEl: HTMLElement;
  private _autoScroll = true;

  // 回调
  onSend?: (text: string) => void;
  onStop?: () => void;

  constructor(container: HTMLElement) {
    this._container = container;

    // 消息区
    const messagesEl = container.querySelector<HTMLElement>(".chat-messages");
    if (!messagesEl) throw new Error("ChatUI: .chat-messages not found");
    this._renderer = new MessageRenderer(messagesEl, CONFIG.avatar);

    // 滚动监听
    messagesEl.addEventListener("scroll", () => {
      const el = messagesEl;
      const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
      this._autoScroll = distFromBottom <= CONFIG.chat.autoScrollThreshold;
      this._toggleScrollHint(!this._autoScroll);
    });

    // 右键菜单：事件委托，捕获 .chat-message--ai / .chat-message--user 的文字
    messagesEl.addEventListener("contextmenu", (e) => {
      const target = e.target as HTMLElement;
      const bubble = target.closest<HTMLElement>(".chat-message--ai, .chat-message--user");
      if (!bubble || !bubble.textContent?.trim()) return;
      e.preventDefault();
      showContextMenu(e.clientX, e.clientY, bubble.textContent.trim());
    });

    // 输入区
    const textarea = container.querySelector<HTMLTextAreaElement>(".chat-input");
    const button = container.querySelector<HTMLButtonElement>(".chat-send-btn");
    if (!textarea || !button)
      throw new Error("ChatUI: .chat-input or .chat-send-btn not found");
    this._input = new InputController(textarea, button);
    this._input.onSend = (text) => this.onSend?.(text);
    this._input.onStop = () => this.onStop?.();

    // 子元素引用（container 范围内元素用 throw，跨模块引用用 graceful）
    const thinkingEl = container.querySelector<HTMLElement>(".chat-thinking");
    if (!thinkingEl) throw new Error("ChatUI: .chat-thinking not found");
    this._thinkingEl = thinkingEl;

    const quickRepliesEl = container.querySelector<HTMLElement>(".chat-quick-replies");
    if (!quickRepliesEl) throw new Error("ChatUI: .chat-quick-replies not found");
    this._quickRepliesEl = quickRepliesEl;

    // 跨模块引用（F2 已知决策），未找到时静默降级
    this._statusBarEl = document.querySelector<HTMLElement>(".status-bar") ?? document.createElement("div");

    const scrollHintEl = container.querySelector<HTMLElement>(".scroll-hint");
    if (!scrollHintEl) throw new Error("ChatUI: .scroll-hint not found");
    this._scrollHintEl = scrollHintEl;

    // 滚动提示点击 + 键盘
    this._scrollHintEl.addEventListener("click", () => {
      this.scrollToBottom();
      this._autoScroll = true;
      this._toggleScrollHint(false);
    });
    this._scrollHintEl.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        this.scrollToBottom();
        this._autoScroll = true;
        this._toggleScrollHint(false);
      }
    });

    // 新会话欢迎语
    this._greetingEl = document.createElement("div");
    this._greetingEl.className = "chat-greeting";
    this._greetingEl.textContent = "灰宝，想和我聊聊天吗？";
    messagesEl.appendChild(this._greetingEl);
  }

  // ===== 消息操作 =====

  appendUserMessage(content: string): void {
    this._greetingEl.style.display = "none";
    this.hideThinking();
    this.hideQuickReplies();
    this._renderer.appendUserMessage(content);
    this._scrollIfAuto();
  }

  appendAIStreamChunk(text: string): void {
    if (!text) return; // 协议要求忽略空 stream 分片
    this.hideThinking();
    this._renderer.appendAIStreamChunk(text);
    this._scrollIfAuto();
  }

  finalizeAIMessage(fullText: string): void {
    this._renderer.finalizeAIMessage(fullText);
    this.endReplyMode();
  }

  appendSystemMessage(
    text: string,
    type: "info" | "warning" | "blocked" = "info"
  ): void {
    this._renderer.appendSystemMessage(text, type);
    this._scrollIfAuto();
  }

  clearMessages(): void {
    this._renderer.clear();
    // renderer.clear() 会清空 .chat-messages，需要重新挂载欢迎语
    const messagesEl = this._container.querySelector(".chat-messages");
    if (messagesEl && !messagesEl.contains(this._greetingEl)) {
      messagesEl.appendChild(this._greetingEl);
    }
    this._greetingEl.style.display = "";
  }

  loadMessages(messages: ChatMessage[]): void {
    this._renderer.loadMessages(messages);
    // renderer.loadMessages() 也会清空容器，需要重新挂载
    const messagesEl = this._container.querySelector(".chat-messages");
    if (messagesEl && !messagesEl.contains(this._greetingEl)) {
      messagesEl.appendChild(this._greetingEl);
    }
    this._greetingEl.style.display = messages.length > 0 ? "none" : "";
    this.scrollToBottom();
  }

  scrollToBottom(): void {
    const el = this._container.querySelector<HTMLElement>(".chat-messages");
    if (el) {
      requestAnimationFrame(() => {
        el.scrollTop = el.scrollHeight;
      });
    }
  }

  // ===== 输入状态控制 =====

  /** AI 回复开始：锁定输入 + 显示停止按钮 */
  setReplyMode(): void {
    this._input.lock();
    this._input.showStopButton();
  }

  /** 立即显示 AI loading 气泡（不等后端 thinking 报文） */
  showAILoading(): void {
    if (this._renderer.isStreaming) return;
    this._renderer.showAILoading();
    this._scrollIfAuto();
  }

  /** AI 回复结束（含中断）：解锁输入 + 恢复发送按钮 */
  endReplyMode(): void {
    // 如果有未固化的流式气泡，丢弃
    if (this._renderer.isStreaming) {
      this._renderer.finalizeAIMessage("");
    }
    this._input.unlock();
  }

  // ===== 思考中 =====

  showThinking(): void {
    if (this._thinkingEl.style.display === "block") return;
    this._thinkingEl.style.display = "block";
  }

  hideThinking(): void {
    if (this._thinkingEl.style.display === "none") return;
    this._thinkingEl.style.display = "none";
  }

  // ===== 快捷回复 =====

  showQuickReplies(replies: string[]): void {
    if (!Array.isArray(replies)) return;
    this._quickRepliesEl.innerHTML = "";
    if (replies.length === 0) {
      this._quickRepliesEl.style.display = "none";
      return;
    }
    for (const reply of replies) {
      const btn = document.createElement("button");
      btn.className = "chat-quick-reply";
      btn.textContent = reply;
      btn.addEventListener("click", () => {
        this.hideQuickReplies();
        this.onSend?.(reply);
      });
      this._quickRepliesEl.appendChild(btn);
    }
    this._quickRepliesEl.style.display = "flex";
  }

  hideQuickReplies(): void {
    this._quickRepliesEl.innerHTML = "";
    this._quickRepliesEl.style.display = "none";
  }

  // ===== 连接状态 =====

  updateConnectionStatus(status: ConnectionStatus): void {
    const indicator = this._statusBarEl.querySelector<HTMLElement>(
      ".status-indicator"
    );
    const text = this._statusBarEl.querySelector<HTMLElement>(
      ".status-text"
    );
    if (!indicator || !text) return;

    if (status === "connected") {
      indicator.className = "status-indicator status-indicator--online";
      indicator.setAttribute("aria-label", "已连接");
      text.textContent = "已连接";
    } else if (status === "connecting") {
      indicator.className = "status-indicator status-indicator--offline";
      indicator.setAttribute("aria-label", "连接中");
      text.textContent = "连接中...";
    } else {
      indicator.className = "status-indicator status-indicator--offline";
      indicator.setAttribute("aria-label", "未连接");
      text.textContent = "未连接 — 请启动后端";
    }
  }

  // ===== 内部 =====

  private _scrollIfAuto(): void {
    if (this._autoScroll) {
      this.scrollToBottom();
    }
  }

  private _toggleScrollHint(show: boolean): void {
    this._scrollHintEl.classList.toggle("scroll-hint--visible", show);
  }
}
