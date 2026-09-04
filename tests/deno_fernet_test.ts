import { assertEquals, assertRejects } from "jsr:@std/assert@1.0.14";
import { FernetCipher } from "../src/security/fernet.ts";

const key = "AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=";

Deno.test("Fernet decrypts Python tokens and round-trips Deno tokens", async () => {
  const cipher = await FernetCipher.fromKey(key);
  const pythonToken =
    "gAAAAABqmhnih5i9oieQHZyW3kwJhIQClJVIM2uXNyZpS8Udhu9THLWwNJ3ItrBPuw2xULIonBUdzBoOmUyQ7zhmL6_pGe6p99Yl0Hj4VtTeoLKjWqiCYEM=";
  assertEquals(await cipher.decrypt(pythonToken), "python-access-token");
  assertEquals(
    await cipher.decrypt(await cipher.encrypt("deno-access-token")),
    "deno-access-token",
  );
  await assertRejects(
    () => cipher.decrypt(pythonToken + "x"),
    TypeError,
    "cannot be decrypted",
  );
});

Deno.test("Fernet rejects malformed keys", async () => {
  await assertRejects(
    () => FernetCipher.fromKey("not-a-key"),
    TypeError,
    "valid Fernet key",
  );
});
