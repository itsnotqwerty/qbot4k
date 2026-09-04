import type { StructuredLogger } from "./logging.ts";

export type ShutdownSignal = "SIGINT" | "SIGTERM";

export interface SignalRegistrar {
  add(signal: ShutdownSignal, listener: () => void): void;
  remove(signal: ShutdownSignal, listener: () => void): void;
}

const denoSignals: SignalRegistrar = {
  add: (signal, listener) => Deno.addSignalListener(signal, listener),
  remove: (signal, listener) => Deno.removeSignalListener(signal, listener),
};

export class ShutdownController {
  readonly abortController = new AbortController();
  private readonly listeners = new Map<ShutdownSignal, () => void>();
  private resolveShutdown!: () => void;
  private readonly shutdown = new Promise<void>((resolve) => {
    this.resolveShutdown = resolve;
  });

  constructor(
    private readonly logger: StructuredLogger,
    private readonly registrar: SignalRegistrar = denoSignals,
  ) {}

  install(): void {
    if (this.listeners.size) return;
    for (const signal of ["SIGINT", "SIGTERM"] as const) {
      const listener = () => this.request(signal);
      this.listeners.set(signal, listener);
      this.registrar.add(signal, listener);
    }
  }

  request(signal: ShutdownSignal = "SIGTERM"): void {
    if (this.abortController.signal.aborted) return;
    this.logger.info("received shutdown signal", { signal });
    this.abortController.abort(signal);
    this.resolveShutdown();
  }

  wait(): Promise<void> {
    return this.shutdown;
  }

  dispose(): void {
    for (const [signal, listener] of this.listeners) {
      this.registrar.remove(signal, listener);
    }
    this.listeners.clear();
  }
}
