import { assertEquals } from "jsr:@std/assert@1.0.14";
import {
  calculateCoordinationCampaign,
  campaignFeatures,
} from "../src/domain/campaigns.ts";

Deno.test("campaign features normalize ASCII tokens and domains", () => {
  const features = campaignFeatures(
    "JOIN now at https://WWW.Bad.Example/deal!",
  );
  assertEquals([...features.tokens].sort(), [
    "bad",
    "deal",
    "example",
    "https",
    "join",
    "now",
    "www",
  ]);
  assertEquals([...features.domains], ["bad.example"]);
});

Deno.test("coordination campaigns require three messages and two actors", async () => {
  const current = {
    observationId: 3,
    text: "Join the giveaway at https://bad.example/deal now",
    userId: 10,
  };
  assertEquals(
    await calculateCoordinationCampaign(current, [{
      ...current,
      observationId: 2,
    }]),
    null,
  );
  assertEquals(
    (await calculateCoordinationCampaign(current, [
      { ...current, observationId: 2, userId: 20 },
      { ...current, observationId: 1, userId: 10 },
    ]))?.messageCount,
    3,
  );
});
