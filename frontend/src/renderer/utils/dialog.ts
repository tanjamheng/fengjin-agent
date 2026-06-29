/**
 * 自定义确认对话框 — 替代原生 window.confirm
 * 统一使用"风堇温馨提示"标题
 */
export function showConfirm(message: string): Promise<boolean> {
  return new Promise((resolve) => {
    // 遮罩
    const overlay = document.createElement("div");
    overlay.className = "dialog-overlay";

    // 卡片
    const card = document.createElement("div");
    card.className = "dialog-card";

    const topBar = document.createElement("div");
    topBar.className = "dialog-topbar";

    const title = document.createElement("div");
    title.className = "dialog-title";
    title.textContent = "✦ 风堇温馨提示";

    const body = document.createElement("div");
    body.className = "dialog-body";
    body.textContent = message;

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

    // Escape 关闭
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        close(false);
        document.removeEventListener("keydown", onKey);
      }
    };
    document.addEventListener("keydown", onKey);

    // 淡入
    requestAnimationFrame(() => {
      overlay.classList.add("dialog-overlay--visible");
    });

    function close(result: boolean): void {
      overlay.classList.remove("dialog-overlay--visible");
      setTimeout(() => {
        document.body.removeChild(overlay);
        resolve(result);
      }, 200);
    }
  });
}
