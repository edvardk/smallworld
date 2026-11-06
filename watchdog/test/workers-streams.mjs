// Node stand-ins for the two Workers-only stream classes the artifact upload uses (imported by the tests that
// need them): FixedLengthStream(length), an identity TransformStream that errors unless exactly `length` bytes
// pass, and crypto.DigestStream(algorithm), a WritableStream whose `digest` promise is the ArrayBuffer hash.
// https://developers.cloudflare.com/workers/runtime-apis/streams/transformstream/#fixedlengthstream
// https://developers.cloudflare.com/workers/runtime-apis/web-crypto/#constructors
import { createHash } from "node:crypto";

export class FixedLengthStream extends TransformStream {
  constructor(length) {
    let seen = 0;
    super({
      transform(chunk, ctl) {
        seen += chunk.byteLength;
        if (seen > length) throw new TypeError("FixedLengthStream: too many bytes");
        ctl.enqueue(chunk);
      },
      flush() {
        if (seen !== length) throw new TypeError("FixedLengthStream: too few bytes");
      },
    });
  }
}

const ALGOS = { "SHA-256": "sha256", "SHA-1": "sha1", "SHA-512": "sha512", MD5: "md5" };

export class DigestStream extends WritableStream {
  constructor(algorithm) {
    const hash = createHash(ALGOS[String(algorithm).toUpperCase()] ?? "unknown");
    let done;
    let fail;
    const digest = new Promise((resolve, reject) => {
      done = resolve;
      fail = reject;
    });
    super({
      write(chunk) {
        hash.update(chunk);
      },
      close() {
        const b = hash.digest();
        done(b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength));
      },
      abort(reason) {
        fail(reason);
      },
    });
    this.digest = digest;
  }
}

export function install() {
  globalThis.FixedLengthStream = FixedLengthStream;
  globalThis.crypto.DigestStream = DigestStream;
}

export function uninstall() {
  delete globalThis.FixedLengthStream;
  delete globalThis.crypto.DigestStream;
}
