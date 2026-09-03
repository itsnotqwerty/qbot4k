import RuntimeStatus from "@/islands/RuntimeStatus.tsx";

export default function Home() {
  return (
    <main class="foundation-shell">
      <header>
        <p class="eyebrow">Operator console</p>
        <h1>QBot4K</h1>
      </header>
      <RuntimeStatus />
    </main>
  );
}
