// better-sqlite3 → bun:sqlite 兼容 shim（bun 暂不支持 better-sqlite3 N-API）
// 覆盖 wigolo 使用的 API：prepare/all/get/run、exec、pragma、transaction、close、loadExtension
import { Database as BunDatabase } from "bun:sqlite";

export default class Database {
  constructor(path, _options) {
    this._db = new BunDatabase(path);
    this._closed = false;
  }

  pragma(sql) {
    // better-sqlite3 语义：返回结果数组（如 [{journal_mode:"wal"}]）
    try {
      return this._db.query(`PRAGMA ${sql}`).all();
    } catch {
      return undefined;
    }
  }

  exec(sql) {
    // better-sqlite3 对空 SQL 是 no-op；bun:sqlite 抛 "SQL string mustn't be blank"
    if (!sql || !sql.trim()) {
      return { changes: 0, lastInsertRowid: 0 };
    }
    return this._db.exec(sql);
  }

  prepare(sql) {
    // better-sqlite3 命名参数用 @name；bun:sqlite 只认 $name/:name/@name 的
    // 键绑定，且对象键必须带前缀。这里统一重写 SQL 并转换绑定对象。
    const bunSql = sql.replace(/@([A-Za-z_][A-Za-z0-9_]*)/g, "$$$1");
    const stmt = this._db.query(bunSql);
    const toBunParams = (params) =>
      params.length === 1 &&
      params[0] !== null &&
      typeof params[0] === "object" &&
      !Array.isArray(params[0])
        ? [
            Object.fromEntries(
              Object.entries(params[0]).map(([k, v]) => [
                k.startsWith("$") ? k : `$${k}`,
                v,
              ]),
            ),
          ]
        : params;
    const call = (fn, params) => fn(...toBunParams(params));
    return {
      all: (...params) => call(stmt.all.bind(stmt), params),
      get: (...params) => {
        const row = call(stmt.get.bind(stmt), params);
        return row ?? undefined;
      },
      run: (...params) => {
        const result = call(stmt.run.bind(stmt), params);
        return {
          changes: Number(result.changes),
          lastInsertRowid: Number(result.lastInsertRowid),
        };
      },
      raw: (...params) => {
        const rows = call(stmt.all.bind(stmt), params);
        return rows.map((r) => Object.values(r));
      },
      columns: () => [],
      iterate: (...params) => stmt.all(...params)[Symbol.iterator](),
    };
  }

  transaction(fn) {
    const self = this;
    return function (...args) {
      self._db.exec("BEGIN");
      try {
        const result = fn.apply(this, args);
        self._db.exec("COMMIT");
        return result;
      } catch (error) {
        self._db.exec("ROLLBACK");
        throw error;
      }
    };
  }

  loadExtension() {
    // bun:sqlite 不支持加载扩展；调用方（wigolo）已容忍失败
    return this;
  }

  close() {
    if (!this._closed) {
      this._closed = true;
      this._db.close();
    }
  }
}
