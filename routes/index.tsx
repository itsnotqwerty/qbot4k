import { AppShell } from "@/components/AppShell.tsx";
import { RuntimePanel } from "@/components/RuntimePanel.tsx";
import DemoCollage from "@/islands/DemoCollage.tsx";

export default function Home() {
  return (
    <AppShell>
      <section class="hero">
        <p class="eyebrow">QBot4K · Discord + Twitch</p>
        <h1>
          The first real <span class="hero-accent">community intelligence</span>
          {" "}
          platform for streamers
        </h1>
        <p class="lede">
          Chat moves fast. QBot4K watches everything, remembers what matters,
          and shows you the evidence — so your community stays yours, on Discord
          and Twitch alike.
        </p>
        <p class="hero-sub">
          Not another ban bot. A live picture of who your community actually is.
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
            A triage queue with severity, SLA age, and reversible sanctions —
            every action confirmed by the provider before it counts.
          </p>
        </article>
        <article>
          <h2>Signals that explain themselves</h2>
          <p>
            Invite velocity, evasion heuristics, and cohort anomalies arrive
            with the evidence attached, not just a red badge.
          </p>
        </article>
        <article>
          <h2>Cases, not loose ends</h2>
          <p>
            Alerts roll into investigations that link accounts, relationships,
            and history — and hand off cleanly between shifts.
          </p>
        </article>
        <article>
          <h2>Live ops for the moments that matter</h2>
          <p>
            Raids, hype spikes, and stream incidents tracked in real time, with
            slow mode and raid guards one click away.
          </p>
        </article>
        <article>
          <h2>Both platforms, one truth</h2>
          <p>
            Discord and Twitch identities link into a single member record, so
            the troll you timed out on stream can't reappear in your server.
          </p>
        </article>
        <article>
          <h2>Tenant-safe by construction</h2>
          <p>
            Every community is isolated, every operator action is audited, and
            no one else's data ever bleeds into yours.
          </p>
        </article>
      </section>
      <DemoCollage />
      <RuntimePanel />
    </AppShell>
  );
}
