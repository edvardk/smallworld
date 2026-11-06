// `node --import ./watchdog/test/register.mjs --test ...`: resolves the Workers-only module
// `cloudflare:sockets` to a stub, so worker.js loads under Node. The tests pass their own fake connect().
import { register } from "node:module";

register("./hooks.mjs", import.meta.url);
