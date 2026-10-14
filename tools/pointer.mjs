// Map a browser pointer to the firmware's 0..65535 absolute range. The image
// may be letterboxed by object-fit: contain, so use the drawn content box.
export function toAbsolute(rect, natural, clientX, clientY) {
  const scale = Math.min(rect.width / natural.width, rect.height / natural.height);
  const width = natural.width * scale, height = natural.height * scale;
  const left = rect.left + (rect.width - width) / 2, top = rect.top + (rect.height - height) / 2;
  const fx = (clientX - left) / width, fy = (clientY - top) / height;
  if (!(fx >= 0 && fx <= 1 && fy >= 0 && fy <= 1)) return null;
  return {x: Math.round(fx * 65535), y: Math.round(fy * 65535)};
}

// Pointer motion arrives far faster than a slow link drains it. Keep only the
// newest position and flush it once the socket has room; never queue history.
export class LatestMotion {
  constructor(send, congested) {
    this.send = send;
    this.congested = congested;
    this.pending = null;
  }
  push(position) { this.pending = position; }
  flush() {
    if (!this.pending || this.congested()) return false;
    this.send(this.pending);
    this.pending = null;
    return true;
  }
  clear() { this.pending = null; }
}
