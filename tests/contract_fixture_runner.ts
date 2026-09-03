type JsonObject = { [key: string]: Json };
type Json = null | boolean | number | string | Json[] | JsonObject;
type Scenario = { id: string; operation: string; input: Json };

export function evaluate(operation: string, value: Json): Json {
  if (operation === "authorize_cases") {
    const cases = value as Array<{
      actor_community_id: number;
      requested_community_id: number;
      required_capability: string;
      granted_capabilities: string[];
    }>;
    return cases.map((entry) => {
      const sameTenant =
        entry.actor_community_id === entry.requested_community_id;
      const hasCapability = entry.granted_capabilities.includes(
        entry.required_capability,
      );
      return {
        authorized: sameTenant && hasCapability,
        reason: !sameTenant
          ? "tenant_mismatch"
          : hasCapability
          ? "allowed"
          : "capability_denied",
      };
    });
  }
  if (operation === "select_tenant") {
    const input = value as { community_id: number; records: JsonObject[] };
    return input.records.filter((record) =>
      record.community_id === input.community_id
    );
  }
  if (operation === "project") {
    const input = value as { fields: string[]; record: JsonObject };
    return Object.fromEntries(
      input.fields.map((field) => [field, input.record[field]]),
    );
  }
  if (operation === "sort_jobs") {
    const jobs = value as Array<{ id: number; priority: number }>;
    return [...jobs].sort((left, right) =>
      right.priority - left.priority || left.id - right.id
    );
  }
  if (operation === "parse_command") {
    const [name, ...arguments_] = (value as string).trim().split(/\s+/);
    return {
      name: name.replace(/^!/, "").toLowerCase(),
      arguments: arguments_,
    };
  }
  if (operation === "normalize_provider") {
    const input = value as JsonObject;
    return {
      external_event_id: String(input.external_event_id),
      platform: (input.platform as string).trim().toLowerCase(),
      username: (input.username as string).trim(),
    };
  }
  if (operation === "normalize_html") {
    return (value as string).replace(/\s+/g, " ").trim();
  }
  throw new Error(`unsupported fixture operation: ${operation}`);
}

if (import.meta.main) {
  const fixture = JSON.parse(await Deno.readTextFile(Deno.args[0])) as {
    scenarios: Scenario[];
  };
  const output = Object.fromEntries(
    fixture.scenarios.map((scenario) => [
      scenario.id,
      evaluate(scenario.operation, scenario.input),
    ]),
  );
  console.log(JSON.stringify(output));
}
