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
