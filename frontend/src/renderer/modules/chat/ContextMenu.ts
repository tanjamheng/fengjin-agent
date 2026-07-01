/**
 * ContextMenu — 消息气泡右键复制菜单
 *
 * 单例模式，全局只有一个实例。show() 创建并定位，hide() 销毁。
 * 通过 CSS 变量与主配色一致。
 */

let _menu: HTMLElement | null = null;
let _resolveOnClose: (() => void) | null = null;

function _create(): HTMLElement {
  const el = document.createElement("div");
  el.className = "context-menu";
  el.setAttribute("role", "menu");
  el.innerHTML = `<button class="context-menu__btn" role="menuitem">复制</button>`;
  // 点击复制按钮
  el.querySelector("button")!.addEventListener("click", () => {
    const text = el.dataset.copyText || "";
    navigator.clipboard.writeText(text).catch(() => {
      // 降级：execCommand（极少需要，但兜底）
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    });
    _hide();
  });
  return el;
}

function _hide(): void {
  if (_menu) {
    _menu.remove();
    _menu = null;
  }
  if (_resolveOnClose) {
    _resolveOnClose();
    _resolveOnClose = null;
  }
  document.removeEventListener("click", _onDocumentClick, true);
  document.removeEventListener("keydown", _onKeydown, true);
}

function _onDocumentClick(e: MouseEvent): void {
  if (_menu && !_menu.contains(e.target as Node)) {
    _hide();
  }
}

function _onKeydown(e: KeyboardEvent): void {
  if (e.key === "Escape") {
    _hide();
  }
}

export function show(x: number, y: number, text: string): void {
  _hide(); // 先关闭旧的
  _menu = _create();
  _menu.dataset.copyText = text;
  document.body.appendChild(_menu);

  // 定位：先 set 再读尺寸，避免溢出窗口
  _menu.style.left = `${x}px`;
  _menu.style.top = `${y}px`;

  const rect = _menu.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 4;
  const maxY = window.innerHeight - rect.height - 4;
  if (rect.right > window.innerWidth) _menu.style.left = `${Math.max(4, maxX)}px`;
  if (rect.bottom > window.innerHeight) _menu.style.top = `${Math.max(4, maxY)}px`;

  // 动画入场
  requestAnimationFrame(() => {
    if (_menu) _menu.classList.add("context-menu--visible");
  });

  // 全局监听关闭（捕获阶段，优先处理）
  document.addEventListener("click", _onDocumentClick, true);
  document.addEventListener("keydown", _onKeydown, true);
}

export function hide(): void {
  _hide();
}
