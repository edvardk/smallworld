export async function resolve(specifier, context, next) {
  if (specifier === "cloudflare:sockets") {
    return { url: new URL("./sockets-stub.mjs", import.meta.url).href, shortCircuit: true };
  }
  return next(specifier, context);
}
