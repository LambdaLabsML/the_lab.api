/** Latest-selection request controller for experiment output.
 * It coalesces a live request for one experiment, aborts it on selection
 * changes/close, and only publishes a response for the still-open key. */
export class OutputController<T> {
  private active: { key: string; controller: AbortController; promise: Promise<void> } | null = null;
  private key: string | null = null;

  open(key: string): void {
    if (this.key !== key) this.active?.controller.abort();
    this.key = key;
  }

  close(): void {
    this.active?.controller.abort();
    this.active = null;
    this.key = null;
  }

  fetch(key: string, load: (signal: AbortSignal) => Promise<T>,
        receive: (data: T) => void, fail: (error: unknown) => void): Promise<void> {
    if (this.key !== key) this.open(key);
    if (this.active?.key === key && !this.active.controller.signal.aborted) return this.active.promise;
    this.active?.controller.abort();
    const controller = new AbortController();
    const promise = load(controller.signal).then(
      (data) => { if (!controller.signal.aborted && this.key === key) receive(data); },
      (error) => { if (!controller.signal.aborted && this.key === key) fail(error); },
    ).finally(() => {
      if (this.active?.controller === controller) this.active = null;
    });
    this.active = { key, controller, promise };
    return promise;
  }
}
