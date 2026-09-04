import { useSignal } from "@preact/signals";

export default function RuntimeStatus() {
  const status = useSignal("Online");

  const check = async () => {
    status.value = "Checking";
    try {
      const response = await fetch("/health/ready");
      status.value = response.ok ? "Ready" : "Degraded";
    } catch {
      status.value = "Unavailable";
    }
  };

  return (
    <div class="runtime-status">
      <output aria-live="polite">{status.value}</output>
      <button type="button" onClick={check}>Check now</button>
      <a href="/health/ready">View status</a>
    </div>
  );
}
