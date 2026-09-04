import { AppShell } from "@/components/AppShell.tsx";
import { RuntimePanel } from "@/components/RuntimePanel.tsx";

export default function Home() {
  return (
    <AppShell>
      <section class="hero">
        <p class="eyebrow">Discord + Twitch operations</p>
        <h1>QBot4K</h1>
        <p class="lede">
          Tenant-isolated community operations with evidence before action.
        </p>
        <div class="hero-actions">
          <a class="button" href="/login">Link Discord</a>
          <a class="text-link" href="/login">Operator login</a>
        </div>
      </section>
      <section class="capability-grid" aria-label="Platform capabilities">
        <article>
          <h2>Moderation command</h2>
          <p>
            Review queues, reversible sanctions, and provider-confirmed
            outcomes.
          </p>
        </article>
        <article>
          <h2>Discord and Twitch</h2>
          <p>
            Installation-bound ingestion and actions with scoped authorization.
          </p>
        </article>
        <article>
          <h2>Operational intelligence</h2>
          <p>
            Explainable signals, evidence, and shift handoffs in one tenant-safe
            workspace.
          </p>
        </article>
      </section>
      <RuntimePanel />
    </AppShell>
  );
}
