import type { ChatMessage } from "../../types/protocol";

/**
 * MessageRenderer — 消息气泡 DOM 渲染
 *
 * 负责创建用户/AI/系统消息的 DOM 元素。
 * 由 ChatUI 调用，不直接操作聊天区容器。
 */

interface AvatarConfig {
  fengjin: string;
  trailblazer: string;
}

export class MessageRenderer {
  private _container: HTMLElement;
  private _aiBubble: HTMLElement | null = null; // 当前流式 AI 气泡
  private _lastMessageTime: number = 0; // 上一条消息时间戳
  private _avatars: AvatarConfig;

  // loading 动画缓冲状态机
  private _loadingDone: boolean = true;       // 渐入动画是否已播完
  private _streamBuffer: string = "";          // 动画期间的 token 缓冲区
  private _loadingTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(container: HTMLElement, avatars: AvatarConfig) {
    this._container = container;
    this._avatars = avatars;
  }

  /** 追加用户消息气泡 */
  appendUserMessage(content: string): void {
    this._finalizeAIBubble(); // 结束流式气泡
    this._maybeAppendTimestamp();

    const wrapper = this._createWrapper("user");
    const bubble = this._createBubble("user", content);
    wrapper.appendChild(bubble);
    wrapper.appendChild(this._createAvatar(this._avatars.trailblazer, "开拓者", "user"));
    this._container.appendChild(wrapper);
    this._updateLastTime();
  }

  /** 立即显示 AI loading 气泡（用户发送后、首 token 到达前） */
  showAILoading(): void {
    this._finalizeAIBubble();
    const wrapper = this._createWrapper("ai");
    wrapper.classList.add("chat-message-wrapper--delayed"); // 等用户气泡 0.1s 渐入完成后再出现
    this._aiBubble = document.createElement("div");
    this._aiBubble.className = "chat-message--ai chat-message--ai-loading";
    this._aiBubble.textContent = "● ● ●";
    wrapper.appendChild(this._aiBubble);
    this._container.appendChild(wrapper);
    this._updateLastTime();

    // 启动缓冲状态机：0.2s 内 token 静默缓冲，动画播完再决定展示文字还是黑点
    this._loadingDone = false;
    this._streamBuffer = "";
    this._loadingTimer = setTimeout(() => {
      this._loadingTimer = null;
      this._loadingDone = true;
      // 动画播完：如果期间有 token 到达，立刻显示缓冲文字
      if (this._streamBuffer && this._aiBubble) {
        const bubble = this._aiBubble;
        bubble.classList.remove("chat-message--ai-loading");
        bubble.textContent = this._streamBuffer;
        // 清除延迟动画类，确保立即可见
        const w = bubble.parentElement;
        if (w) w.classList.remove("chat-message-wrapper--delayed");
      }
    }, 200);
  }

  /** 流式追加 AI 文字分片 */
  appendAIStreamChunk(text: string): void {
    if (!this._aiBubble) {
      // 首 chunk 到达但 loading 未创建（竞态兜底）
      this.showAILoading();
    }

    // 动画未播完 → 静默缓冲 token，不修改 DOM
    if (!this._loadingDone) {
      this._streamBuffer += text;
      return;
    }

    // 动画已播完 → 正常流式显示
    const bubble = this._aiBubble!;
    if (bubble.classList.contains("chat-message--ai-loading")) {
      bubble.classList.remove("chat-message--ai-loading");
      // 清除延迟渐入类（timer 中可能已清，此处防御）
      const wrapper = bubble.parentElement;
      if (wrapper) wrapper.classList.remove("chat-message-wrapper--delayed");
      bubble.textContent = "";
    }
    bubble.textContent += text;
  }

  /** 固化流式 AI 消息 */
  finalizeAIMessage(fullText: string): void {
    this._cancelLoadingTimer();
    if (this._aiBubble) {
      const isLoading = this._aiBubble.classList.contains("chat-message--ai-loading");
      // loading 气泡（仅含黑点/缓冲，无最终文本）视为无内容，直接移除
      const text = fullText || (isLoading ? "" : this._aiBubble.textContent || "");
      if (!text) {
        const wrapper = this._aiBubble.parentElement;
        if (wrapper) wrapper.remove();
        this._aiBubble = null;
        this._loadingDone = true;
        return;
      }
      this._aiBubble.classList.remove("chat-message--ai-loading");
      this._aiBubble.textContent = text;
      this._aiBubble = null;
      this._loadingDone = true;
    }
  }

  /** 追加系统提示消息 */
  appendSystemMessage(
    text: string,
    type: "info" | "warning" | "blocked" = "info"
  ): void {
    this._finalizeAIBubble();

    const el = document.createElement("div");
    el.className = "chat-message--system";
    if (type === "blocked") {
      el.classList.add("chat-message--system--blocked");
      el.setAttribute("role", "alert");
    } else if (type === "warning") {
      el.classList.add("chat-message--system--warning");
      el.setAttribute("role", "alert");
    }
    el.textContent = text;
    this._container.appendChild(el);
  }

  /** 加载历史消息（批量渲染） */
  loadMessages(messages: ChatMessage[]): void {
    this.clear();
    if (!Array.isArray(messages)) return;
    for (const msg of messages) {
      const wrapper = this._createWrapper(msg.role);
      const bubble = this._createBubble(msg.role, msg.content);
      wrapper.appendChild(bubble);
      if (msg.role === "user") {
        wrapper.appendChild(this._createAvatar(this._avatars.trailblazer, "开拓者", "user"));
      }
      this._container.appendChild(wrapper);
    }
    if (messages.length > 0) {
      const last = messages[messages.length - 1];
      this._lastMessageTime = new Date(last.timestamp).getTime();
    }
  }

  /** 清空所有消息 */
  clear(): void {
    this._cancelLoadingTimer();
    this._aiBubble = null;
    this._loadingDone = true;
    this._streamBuffer = "";
    this._lastMessageTime = 0;
    this._container.innerHTML = "";
  }

  /** 当前是否有未固化的流式气泡 */
  get isStreaming(): boolean {
    return this._aiBubble !== null;
  }

  // ---- 私有 ----

  private _finalizeAIBubble(): void {
    // 若流式气泡未收到 end 包但新消息到来，移除 DOM 元素避免"幽灵气泡"
    this._cancelLoadingTimer();
    this._loadingDone = true;
    this._streamBuffer = "";
    if (this._aiBubble) {
      const wrapper = this._aiBubble.parentElement;
      if (wrapper) wrapper.remove();
      this._aiBubble = null;
    }
  }

  private _cancelLoadingTimer(): void {
    if (this._loadingTimer !== null) {
      clearTimeout(this._loadingTimer);
      this._loadingTimer = null;
    }
  }

  private _createWrapper(role: string): HTMLElement {
    const wrapper = document.createElement("div");
    wrapper.className = `chat-message-wrapper chat-message-wrapper--${role}`;

    // AI/assistant 消息：左侧加风堇头像
    if (role === "ai" || role === "assistant") {
      const avatar = this._createAvatar(this._avatars.fengjin, "风堇", "ai");
      wrapper.appendChild(avatar);
    }

    return wrapper;
  }

  /** 创建圆形头像 img 元素（加载失败时保留白色圆形占位） */
  private _createAvatar(src: string, alt: string, side: "ai" | "user"): HTMLElement {
    const img = document.createElement("img");
    img.className = `chat-avatar chat-avatar--${side}`;
    img.src = src;
    img.alt = alt;
    img.loading = "lazy";
    // 加载失败时保留圆形占位
    img.onerror = () => {
      img.style.display = "none";
    };
    return img;
  }

  private _createBubble(role: string, content: string): HTMLElement {
    const bubble = document.createElement("div");
    bubble.className = `chat-message--${role}`;
    bubble.textContent = content;
    return bubble;
  }

  private _maybeAppendTimestamp(): void {
    const now = Date.now();
    if (this._lastMessageTime > 0 && now - this._lastMessageTime > 5 * 60 * 1000) {
      const timeEl = document.createElement("div");
      timeEl.className = "chat-message__time";
      timeEl.textContent = this._formatTime(now);
      this._container.appendChild(timeEl);
    }
  }

  private _updateLastTime(): void {
    this._lastMessageTime = Date.now();
  }

  private _formatTime(ts: number): string {
    const d = new Date(ts);
    const now = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    if (
      d.getFullYear() === now.getFullYear() &&
      d.getMonth() === now.getMonth() &&
      d.getDate() === now.getDate()
    ) {
      return `${hh}:${mm}`;
    }
    const month = String(d.getMonth() + 1).padStart(2, "0");
    const day = String(d.getDate()).padStart(2, "0");
    return `${month}-${day} ${hh}:${mm}`;
  }
}
