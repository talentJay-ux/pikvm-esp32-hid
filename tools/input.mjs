// Keep pointer traffic bounded when HTTP is slower than browser input events.
const bounded = value => Math.max(-127, Math.min(127, value));

export class InputQueue {
  constructor(send, lease, onError) {
    this.send = send;
    this.lease = lease;
    this.onError = onError;
    this.pending = [];
    this.running = false;
  }
  clear() { this.pending.length = 0; }
  push(data) {
    const lease = this.lease();
    if (!lease) return;
    const last = this.pending.at(-1);
    if (last?.lease === lease && last.data.kind === data.kind && ['move', 'wheel'].includes(data.kind)) {
      last.data.dy = bounded(last.data.dy + data.dy);
      if (data.kind === 'move') last.data.dx = bounded(last.data.dx + data.dx);
    } else {
      this.pending.push({lease, data: {...data}});
    }
    this.drain();
  }
  async drain() {
    if (this.running) return;
    this.running = true;
    try {
      while (this.pending.length) {
        const item = this.pending.shift();
        if (item.lease !== this.lease()) continue;
        try { await this.send(item.data, item.lease); }
        catch (error) { this.onError(error, item.lease); }
      }
    } finally { this.running = false; }
  }
}
