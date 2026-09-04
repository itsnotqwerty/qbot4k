const encoder = new TextEncoder();
const decoder = new TextDecoder();
const FERNET_VERSION = 0x80;

function decodeBase64Url(value: string): Uint8Array {
  const normalized = value.replaceAll("-", "+").replaceAll("_", "/");
  const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, "=");
  try {
    return Uint8Array.from(
      atob(padded),
      (character) => character.charCodeAt(0),
    );
  } catch {
    throw new TypeError("credential encryption key must be a valid Fernet key");
  }
}

function encodeBase64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_");
}

function ownedBytes(value: Uint8Array): Uint8Array<ArrayBuffer> {
  const copy = new Uint8Array(value.byteLength);
  copy.set(value);
  return copy;
}

async function importKeys(
  key: Uint8Array,
): Promise<{ signing: CryptoKey; encryption: CryptoKey }> {
  if (key.length !== 32) {
    throw new TypeError("credential encryption key must be a valid Fernet key");
  }
  const signing = await crypto.subtle.importKey(
    "raw",
    ownedBytes(key.slice(0, 16)),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign", "verify"],
  );
  const encryption = await crypto.subtle.importKey(
    "raw",
    ownedBytes(key.slice(16)),
    { name: "AES-CBC" },
    false,
    ["encrypt", "decrypt"],
  );
  return { signing, encryption };
}

function timestampBytes(date: Date): Uint8Array {
  const bytes = new Uint8Array(8);
  new DataView(bytes.buffer).setBigUint64(
    0,
    BigInt(Math.floor(date.valueOf() / 1000)),
    false,
  );
  return bytes;
}

function concatenate(...parts: readonly Uint8Array[]): Uint8Array<ArrayBuffer> {
  const result = new Uint8Array(
    parts.reduce((length, part) => length + part.length, 0),
  );
  let offset = 0;
  for (const part of parts) {
    result.set(part, offset);
    offset += part.length;
  }
  return result;
}

export class FernetCipher {
  private constructor(
    private readonly signingKey: CryptoKey,
    private readonly encryptionKey: CryptoKey,
  ) {}

  static async fromKey(encodedKey: string): Promise<FernetCipher> {
    const { signing, encryption } = await importKeys(
      decodeBase64Url(encodedKey.trim()),
    );
    return new FernetCipher(signing, encryption);
  }

  async encrypt(value: string, now = new Date()): Promise<string> {
    const iv = crypto.getRandomValues(new Uint8Array(16));
    const ciphertext = new Uint8Array(
      await crypto.subtle.encrypt(
        { name: "AES-CBC", iv: ownedBytes(iv) },
        this.encryptionKey,
        ownedBytes(encoder.encode(value)),
      ),
    );
    const signed = concatenate(
      Uint8Array.of(FERNET_VERSION),
      timestampBytes(now),
      iv,
      ciphertext,
    );
    const signature = new Uint8Array(
      await crypto.subtle.sign("HMAC", this.signingKey, signed),
    );
    return encodeBase64Url(concatenate(signed, signature));
  }

  async decrypt(token: string): Promise<string> {
    let bytes: Uint8Array;
    try {
      bytes = decodeBase64Url(token.trim());
    } catch {
      throw new TypeError("installation credentials cannot be decrypted");
    }
    if (bytes.length < 73 || bytes[0] !== FERNET_VERSION) {
      throw new TypeError("installation credentials cannot be decrypted");
    }
    const signed = ownedBytes(bytes.slice(0, -32));
    const signature = ownedBytes(bytes.slice(-32));
    if (
      !await crypto.subtle.verify("HMAC", this.signingKey, signature, signed)
    ) {
      throw new TypeError("installation credentials cannot be decrypted");
    }
    try {
      const plaintext = await crypto.subtle.decrypt(
        { name: "AES-CBC", iv: ownedBytes(bytes.slice(9, 25)) },
        this.encryptionKey,
        ownedBytes(bytes.slice(25, -32)),
      );
      return decoder.decode(plaintext);
    } catch {
      throw new TypeError("installation credentials cannot be decrypted");
    }
  }
}
