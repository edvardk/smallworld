// A D1 binding over node:sqlite (Node >= 22.13), for the panel tests and the UI smoke test: real SQL, so a query
// that SQLite (and D1, which is SQLite) would reject fails here too. Subset: prepare().bind().all()/run()/first(),
// batch(). Counts rows written like D1's meta.changes.
import { DatabaseSync } from "node:sqlite";

const value = (v) => (v === undefined ? null : typeof v === "boolean" ? (v ? 1 : 0) : v);

export class SqliteD1 {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    this.written = 0;
    this.statements = [];
  }

  prepare(sql) {
    const d1 = this;
    const make = (args) => ({
      bind: (...a) => make(a.map(value)),
      all: async () => d1.exec(sql, args),
      run: async () => d1.exec(sql, args),
      first: async () => (await d1.exec(sql, args)).results[0] ?? null,
    });
    return make([]);
  }

  exec(sql, args) {
    this.statements.push(sql);
    const stmt = this.db.prepare(sql);
    if (/^\s*(SELECT|WITH|PRAGMA)\b/i.test(sql) || /\bRETURNING\b/i.test(sql)) {
      return { results: stmt.all(...args), meta: { changes: 0 } };
    }
    const r = stmt.run(...args);
    this.written += Number(r.changes);
    return { results: [], meta: { changes: Number(r.changes) } };
  }

  async batch(stmts) {
    const out = [];
    for (const s of stmts) out.push(await s.run());
    return out;
  }
}
