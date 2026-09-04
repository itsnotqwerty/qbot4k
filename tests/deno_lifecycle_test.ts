import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  ShutdownController,
  type ShutdownSignal,
  type SignalRegistrar,
} from "../src/core/lifecycle.ts";
import { type LogRecord, StructuredLogger } from "../src/core/logging.ts";

class FakeSignals implements SignalRegistrar {
  readonly listeners = new Map<ShutdownSignal, () => void>();
  add(signal: ShutdownSignal, listener: () => void): void {
    this.listeners.set(signal, listener);
  }
  remove(signal: ShutdownSignal, _listener: () => void): void {
    this.listeners.delete(signal);
  }
  send(signal: ShutdownSignal): void {
    this.listeners.get(signal)?.();
  }
}

Deno.test("shutdown controller handles one signal and restores listeners", async () => {
  const signals = new FakeSignals();
  const records: LogRecord[] = [];
  const lifecycle = new ShutdownController(
    new StructuredLogger(
      "qbot4k.test",
      "INFO",
      (record) => records.push(record),
    ),
    signals,
  );
  lifecycle.install();
  signals.send("SIGTERM");
  await lifecycle.wait();

  assertEquals(lifecycle.abortController.signal.aborted, true);
  assertEquals(records[0].context, { signal: "SIGTERM" });
  lifecycle.dispose();
  assertEquals(signals.listeners.size, 0);
});
