import { assertEquals } from "jsr:@std/assert@1.0.14";
import { TokenStore, updateEnvironmentValue } from "../src/security/token_store.ts";

Deno.test("environment updates replace one exact key and preserve unrelated values", () => {
  assertEquals(
    updateEnvironmentValue(
      "QBOT_TWITCH_BOT_TOKEN=old\nOTHER=value\n",
      "QBOT_TWITCH_BOT_TOKEN",
      "new",
    ),
    "QBOT_TWITCH_BOT_TOKEN=new\nOTHER=value\n",
  );
  assertEquals(
    updateEnvironmentValue("OTHER=value", "NEW_KEY", "new"),
    "OTHER=value\nNEW_KEY=new\n",
  );
});

Deno.test("refreshed Twitch tokens persist atomically", async () => {
  const directory = await Deno.makeTempDir();
  try {
    const path = `${directory}/qbot4k.env`;
    await Deno.writeTextFile(
      path,
      "OTHER=value\nQBOT_TWITCH_REFRESH_TOKEN=old-refresh\n",
    );
    const store = new TokenStore(path);
    await Promise.all([
      store.persistRefreshedTwitchTokens("first-access", "first-refresh"),
      store.persistRefreshedTwitchTokens("final-access", "final-refresh"),
    ]);
    assertEquals(
      await Deno.readTextFile(path),
      "OTHER=value\nQBOT_TWITCH_REFRESH_TOKEN=final-refresh\nQBOT_TWITCH_BOT_TOKEN=final-access\n",
    );
    assertEquals((await Deno.stat(path)).mode! & 0o777, 0o640);
  } finally {
    await Deno.remove(directory, { recursive: true });
  }
});
