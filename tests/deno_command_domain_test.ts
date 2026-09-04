import { assertEquals, assertThrows } from "jsr:@std/assert@1.0.14";
import {
  formatCommandTemplate,
  normalizeCustomCommandName,
  parseCommand,
  parseHttpSelectorSpec,
  renderCommandReply,
} from "../src/domain/command_domain.ts";

Deno.test("command parsing preserves prefixes, casefolding, and arguments", () => {
  assertEquals(parseCommand("  !Verify fixture evidence  "), [
    "verify",
    ["fixture", "evidence"],
  ]);
  assertEquals(parseCommand("?Straße value", "?"), ["strasse", ["value"]]);
  assertEquals(parseCommand("plain text"), null);
  assertEquals(parseCommand("!   "), null);
});

Deno.test("custom command names exclude reserved commands", () => {
  assertEquals(normalizeCustomCommandName("!!Status"), "status");
  assertEquals(normalizeCustomCommandName("!ADDCom"), "");
  assertEquals(normalizeCustomCommandName("credit"), "");
});

Deno.test("command templates preserve deterministic range and formatting behavior", () => {
  const selectUpper = (_lower: number, upper: number) => upper;
  assertEquals(
    formatCommandTemplate(
      "hello {author_username} roll={5..1} idx={0..{query}}",
      { author_username: "sam", query: "limit 49" },
      selectUpper,
    ),
    "hello sam roll=5 idx=49",
  );
  assertEquals(
    formatCommandTemplate("idx={0..{query}}", { query: "oops" }, selectUpper),
    "idx=0",
  );
  assertEquals(
    formatCommandTemplate("value={0..9999999}", {}, selectUpper),
    "value=1000000",
  );
  assertEquals(formatCommandTemplate("unknown={missing}"), "unknown={missing}");
});

Deno.test("HTTP command macros preserve selectors aliases caching and URL queries", () => {
  const requests: string[] = [];
  const response = (method: string, url: string) => {
    requests.push(`${method} ${url}`);
    return '{"data":{"name":"Sam","scores":[5,7,9],"active":true}}';
  };
  assertEquals(
    formatCommandTemplate(
      "{GET}(https://example.test/search?q={query})[name:data.name;score:data.scores.2] {name}:{score} {GET}(https://example.test/search?q={query})[data.active]",
      { query: "hello world" },
      (_lower, upper) => upper,
      response,
    ),
    " Sam:9 true",
  );
  assertEquals(requests, ["GET https://example.test/search?q=hello+world"]);
  assertEquals(parseHttpSelectorSpec("first=data.name; second:data.scores.0"), [
    "first:data.name",
    "second:data.scores.0",
  ]);
});

Deno.test("HTTP command macro failures preserve alias placeholders", () => {
  assertEquals(
    formatCommandTemplate(
      "prefix {GET}(https://example.test/fail)[name:data.name] {name}",
      {},
      (_lower, upper) => upper,
      () => null,
    ),
    "prefix  {name}",
  );
});

Deno.test("Discord command rendering preserves safe mention envelopes", () => {
  assertEquals(
    renderCommandReply({
      card: {
        title: "Status",
        description: "Ready",
        fields: [{ name: "Queue", value: "Empty" }],
        footer: "QBot4K",
        color: 0x123456,
      },
    }, "discord"),
    {
      allowed_mentions: { parse: [] },
      embeds: [{
        title: "Status",
        description: "Ready",
        color: 0x123456,
        fields: [{ name: "Queue", value: "Empty", inline: false }],
        footer: { text: "QBot4K" },
      }],
    },
  );
  assertEquals(
    renderCommandReply({
      card: { title: "Status", description: "Ready" },
      textOnly: true,
    }, "discord"),
    {
      allowed_mentions: { parse: [] },
      content: "Ready",
    },
  );
});

Deno.test("Twitch command rendering flattens card details", () => {
  assertEquals(
    renderCommandReply({
      card: {
        title: "Status",
        description: "Ready",
        fields: [{ name: "Queue", value: "Empty", inline: true }],
        footer: "QBot4K",
      },
    }, "twitch"),
    "Status | Ready | Queue: Empty | QBot4K",
  );
  assertEquals(
    renderCommandReply({
      card: { title: "Status", description: "Ready" },
    }, "twitch"),
    "Ready",
  );
  assertThrows(
    () => renderCommandReply({ card: { title: "", description: "" } }, "irc"),
    TypeError,
    "unsupported command platform",
  );
});
