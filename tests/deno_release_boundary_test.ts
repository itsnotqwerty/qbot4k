import { assertEquals, assertFalse } from "jsr:@std/assert@1.0.14";

async function collectFiles(root: string): Promise<string[]> {
  const files: string[] = [];
  for await (const entry of Deno.readDir(root)) {
    const path = `${root}/${entry.name}`;
    if (entry.isDirectory) {
      files.push(...await collectFiles(path));
    } else if (entry.isFile) {
      files.push(path);
    }
  }
  return files;
}

Deno.test("release contains no Python source, tests, installer, or requirements", async () => {
  const files = (await Promise.all(
    ["src", "tests", "deploy"].map(collectFiles),
  )).flat();
  assertEquals(files.filter((path) => path.endsWith(".py")), []);

  for (const path of ["requirements.txt", "requirements-dev.txt"]) {
    assertFalse(
      await exists(path),
      `${path} must not ship in the Deno release`,
    );
  }

  for (const path of ["deno.json", ".github/workflows/port-contracts.yml"]) {
    const source = await Deno.readTextFile(path);
    assertFalse(/\.py\b|setup-python|requirements\.txt|\bpip\b/u.test(source));
  }
});

Deno.test("SQLite APIs remain confined to the offline importer", async () => {
  const productionFiles = [
    "main.ts",
    "runtime.ts",
    "cli.ts",
    ...(await collectFiles("src")).filter((path) => path.endsWith(".ts")),
  ].filter((path) => path !== "src/ops/database_transfer.ts");

  for (const path of productionFiles) {
    const source = await Deno.readTextFile(path);
    assertFalse(
      /node:sqlite|\bDatabaseSync\b/u.test(source),
      `${path} depends on SQLite outside the offline importer`,
    );
  }
});

async function exists(path: string): Promise<boolean> {
  try {
    await Deno.stat(path);
    return true;
  } catch (error) {
    if (error instanceof Deno.errors.NotFound) return false;
    throw error;
  }
}
