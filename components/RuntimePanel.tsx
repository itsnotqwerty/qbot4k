import RuntimeStatus from "@/islands/RuntimeStatus.tsx";

export function RuntimePanel() {
  return (
    <section class="runtime-panel" aria-labelledby="runtime-status-title">
      <div>
        <p class="section-label">Foundation service</p>
        <h2 id="runtime-status-title">Deno + Fresh runtime</h2>
        <p>Server-rendered and ready for the dashboard transition.</p>
      </div>
      <RuntimeStatus />
    </section>
  );
}
