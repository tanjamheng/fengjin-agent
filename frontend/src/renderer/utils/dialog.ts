/**
 * 自定义确认对话框 — 替代原生 window.confirm
 * 统一使用"风堇温馨提示"标题
 *
 * 无障碍设计：
 * - role="dialog" + aria-modal="true" + aria-labelledby + aria-describedby
 * - 焦点陷阱：Tab/Shift+Tab 在对话框内循环
 * - 打开时自动聚焦确认按钮
 * - 关闭时焦点恢复到触发元素（通过 document.activeElement 记录）
 */

export function showConfirm(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    // 记录触发焦点
    const triggerEl = document.activeElement as HTMLElement | null;

    // 遮罩
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    // 卡片
    const card = document.createElement("div");
    card.className = "dialog-card";

    const topBar = document.createElement("div");
    topBar.className = "dialog-topbar";

    const title = document.createElement("div");
    title.className = "dialog-title";
    title.textContent = "✦ 风堇温馨提示";
    title.id = "dialog-title";
    overlay.setAttribute("aria-labelledby", "dialog-title");

    const body = document.createElement("div");
    body.className = "dialog-body";
    body.textContent = message;
    body.id = "dialog-body";
    overlay.setAttribute("aria-describedby", "dialog-body");

    const actions = document.createElement("div");
    actions.className = "dialog-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "dialog-btn dialog-btn--cancel";
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", () => close(false));

    const confirmBtn = document.createElement("button");
    confirmBtn.className = "dialog-btn dialog-btn--confirm";
    confirmBtn.textContent = "确认";
    confirmBtn.addEventListener("click", () => close(true));

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    card.appendChild(topBar);
    card.appendChild(title);
    card.appendChild(body);
    card.appendChild(actions);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // 点击遮罩关闭
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(false);
    });

    // 焦点陷阱
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close(false);
        return;
      }
      if (e.key === "Tab") {
        const focusable = overlay.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    document.addEventListener("keydown", onKey);

    // 淡入
    requestAnimationFrame(() => {
      overlay.classList.add("dialog-overlay--visible");
    });

    // 初始焦点到确认按钮
    requestAnimationFrame(() => {
      confirmBtn.focus();
    });

    function close(result: boolean): void {
      // 先移除键盘监听，防止重复关闭
      document.removeEventListener("keydown", onKey);
      overlay.classList.remove("dialog-overlay--visible");
      setTimeout(() => {
        if (overlay.parentElement) {
          document.body.removeChild(overlay);
        }
        resolve(result);
        // 焦点恢复到触发元素
        if (triggerEl && triggerEl !== document.body) {
          triggerEl.focus();
        }
      }, 200);
    }
  });
}

/**
 * 自定义输入对话框 — 替代原生 window.prompt
 * 统一使用"风堇温馨提示"标题
 *
 * 布局：标题栏 + 说明文字 + input + 确认/取消按钮
 * 支持 Enter 提交、Escape 取消、焦点陷阱
 *
 * @param message  提示文字
 * @param initial  输入框默认值（可选）
 * @param maxLength  最大输入长度（默认 100）
 * @returns 用户输入的文本，取消返回 null
 */
export function showPrompt(message: string, initial: string = "", maxLength: number = 100): Promise<string | null> {
  return new Promise((resolve) => {
    const triggerEl = document.activeElement as HTMLElement | null;

    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";
    overlay.setAttribute("role", "dialog");
    overlay.setAttribute("aria-modal", "true");

    const card = document.createElement("div");
    card.className = "dialog-card";

    const topBar = document.createElement("div");
    topBar.className = "dialog-topbar";

    const title = document.createElement("div");
    title.className = "dialog-title";
    title.textContent = "✦ 风堇温馨提示";
    title.id = "dialog-title";
    overlay.setAttribute("aria-labelledby", "dialog-title");

    const body = document.createElement("div");
    body.className = "dialog-body";
    body.textContent = message;
    body.id = "dialog-body";
    overlay.setAttribute("aria-describedby", "dialog-body");

    const input = document.createElement("input");
    input.type = "text";
    input.className = "dialog-input";
    input.value = initial;
    input.maxLength = maxLength;
    input.setAttribute("aria-label", "新标题");

    const actions = document.createElement("div");
    actions.className = "dialog-actions";

    const cancelBtn = document.createElement("button");
    cancelBtn.className = "dialog-btn dialog-btn--cancel";
    cancelBtn.textContent = "取消";
    cancelBtn.addEventListener("click", () => close(null));

    const confirmBtn = document.createElement("button");
    confirmBtn.className = "dialog-btn dialog-btn--confirm";
    confirmBtn.textContent = "确认";
    confirmBtn.addEventListener("click", () => {
      const val = input.value.trim();
      if (!val) return; // 空输入不提交
      close(val);
    });

    actions.appendChild(cancelBtn);
    actions.appendChild(confirmBtn);
    card.appendChild(topBar);
    card.appendChild(title);
    card.appendChild(body);
    card.appendChild(input);
    card.appendChild(actions);
    overlay.appendChild(card);
    document.body.appendChild(overlay);

    // 点击遮罩关闭
    overlay.addEventListener("click", (e) => {
      if (e.target === overlay) close(null);
    });

    // 键盘：Enter 提交，Escape 取消，Tab 焦点陷阱
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close(null);
        return;
      }
      if (e.key === "Enter") {
        const val = input.value.trim();
        if (!val) return;
        close(val);
        return;
      }
      if (e.key === "Tab") {
        const focusable = overlay.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        );
        if (focusable.length === 0) return;
        const first = focusable[0];
        const last = focusable[focusable.length - 1];
        if (e.shiftKey) {
          if (document.activeElement === first) {
            e.preventDefault();
            last.focus();
          }
        } else {
          if (document.activeElement === last) {
            e.preventDefault();
            first.focus();
          }
        }
      }
    };
    document.addEventListener("keydown", onKey);

    requestAnimationFrame(() => {
      overlay.classList.add("dialog-overlay--visible");
    });

    // 初始焦点到输入框，并全选文本
    requestAnimationFrame(() => {
      input.focus();
      input.select();
    });

    function close(result: string | null): void {
      document.removeEventListener("keydown", onKey);
      overlay.classList.remove("dialog-overlay--visible");
      setTimeout(() => {
        if (overlay.parentElement) {
          document.body.removeChild(overlay);
        }
        resolve(result);
        if (triggerEl && triggerEl !== document.body) {
          triggerEl.focus();
        }
      }, 200);
    }
  });
}
