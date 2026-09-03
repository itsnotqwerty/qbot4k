import { useSignal } from "@preact/signals";

export default function RuntimeStatus() {
  const checked = useSignal(false);
  return (
    <section class="runtime-status" aria-labelledby="runtime-status-title">
      <div>
        <h2 id="runtime-status-title">Runtime</h2>
        <output>{checked.value ? "Ready" : "Online"}</output>
      </div>
      <button type="button" onClick={() => checked.value = true}>
        Check
      </button>
    </section>
  );
}
