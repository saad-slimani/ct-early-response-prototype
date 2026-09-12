export const PLANE_AXES = {axial: [0, 1, 2], coronal: [0, 2, 1], sagittal: [1, 2, 0]};

export function zoomTransform(zoom, pan, factor, anchor = [0, 0]) {
  const next = Math.max(.25, Math.min(12, zoom * factor));
  const ratio = next / zoom;
  return {zoom: next, pan: pan.map((value, i) => anchor[i] - (anchor[i] - value) * ratio)};
}

export function wheelPixels(event, height = 720) {
  return event.deltaY * (event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? height : 1);
}

export function nextFocus(focused, selected) {
  return focused === selected ? null : selected;
}

export function isTextEditing(target) {
  return !!target.closest('textarea,[contenteditable]:not([contenteditable="false"]),input:not([type="range"]):not([type="checkbox"]):not([type="radio"]):not([type="button"]):not([type="file"])');
}

export class ViewerFullscreen {
  constructor(target, onChange, onFallback = () => {}) {
    this.target = target;
    this.document = target.ownerDocument;
    this.onChange = onChange;
    this.onFallback = onFallback;
    this.expanded = false;
    this.pending = false;
    this.disposed = false;
    this.changed = () => this.sync();
    for (const event of ['fullscreenchange', 'webkitfullscreenchange']) this.document.addEventListener(event, this.changed);
    this.sync();
  }
  get element() { return this.document.fullscreenElement || this.document.webkitFullscreenElement; }
  get active() { return this.expanded || this.element === this.target; }
  sync() {
    this.target.classList.toggle('annotation-fullscreen', this.active);
    if (!this.disposed) this.onChange({active: this.active, expanded: this.expanded, pending: this.pending});
  }
  async toggle() {
    if (this.pending || this.disposed) return;
    if (this.active) return this.exit();
    this.pending = true;
    this.sync();
    try {
      const request = this.target.requestFullscreen || this.target.webkitRequestFullscreen;
      if (!request) throw new Error('Fullscreen API unavailable');
      // Invoke directly from the click/key handler to retain browser user activation.
      await request.call(this.target);
    } catch {
      if (!this.disposed) {
        this.expanded = true;
        this.onFallback();
      }
    } finally {
      this.pending = false;
      if (this.disposed) await this.exit();
      else this.sync();
    }
  }
  async exit() {
    if (this.pending) return;
    this.pending = true;
    this.expanded = false;
    try {
      if (this.element === this.target) {
        const exit = this.document.exitFullscreen || this.document.webkitExitFullscreen;
        await exit.call(this.document);
      }
    } finally {
      this.pending = false;
      this.sync();
    }
  }
  async destroy() {
    this.disposed = true;
    for (const event of ['fullscreenchange', 'webkitfullscreenchange']) this.document.removeEventListener(event, this.changed);
    await this.exit();
  }
}

export class UndoQueue {
  constructor(canRun, undoOne) {
    this.canRun = canRun; this.undoOne = undoOne; this.pending = 0; this.running = false;
  }
  clear() { this.pending = 0; }
  request() { this.pending = Math.min(this.pending + 1, 100); return this.drain(); }
  async drain() {
    if (this.running || !this.canRun()) return;
    this.running = true;
    try {
      while (this.pending && this.canRun()) {
        this.pending--;
        await this.undoOne();
      }
    } catch (error) { this.clear(); throw error; }
    finally { this.running = false; }
  }
}
