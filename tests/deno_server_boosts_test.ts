import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  detectServerBoostSuccess,
  isServerBoostConfirmation,
  serverBoostCommandName,
} from "../src/domain/server_boosts.ts";

Deno.test("server boost commands prefer literal content and accept interactions", () => {
  assertEquals(serverBoostCommandName(" /BUMP now", "boop"), "/bump");
  assertEquals(serverBoostCommandName("", "/BoOp"), "/boop");
  assertEquals(serverBoostCommandName("hello", "other"), null);
});

Deno.test("server boost success uses content embeds and interaction fallback", () => {
  assertEquals(detectServerBoostSuccess("Bump done!"), "/bump");
  assertEquals(
    detectServerBoostSuccess("", "boop", "Completed successfully!"),
    "/boop",
  );
  assertEquals(detectServerBoostSuccess("success", "unknown"), null);
});

Deno.test("server boost confirmations require a bot author", () => {
  assertEquals(
    isServerBoostConfirmation({
      contentRaw: "Bump successful!",
      metadata: { author_is_bot: true },
    }),
    true,
  );
  assertEquals(
    isServerBoostConfirmation({
      contentRaw: "Bump successful!",
      metadata: { author_is_bot: false },
    }),
    false,
  );
});
