import { DatabaseSync } from 'node:sqlite';
import { mkdirSync, chmodSync } from 'node:fs';
import { dirname } from 'node:path';
import { Pool } from 'pg';

const ddl = `
CREATE TABLE IF NOT EXISTS bridge_sessions (installation TEXT NOT NULL, subject TEXT NOT NULL, cookie TEXT NOT NULL, expires DOUBLE PRECISION NOT NULL, PRIMARY KEY(installation,subject));
CREATE TABLE IF NOT EXISTS bridge_session_claims (installation TEXT NOT NULL, subject TEXT NOT NULL, PRIMARY KEY(installation,subject));
CREATE TABLE IF NOT EXISTS bridge_ids (installation TEXT NOT NULL, kind TEXT NOT NULL, original TEXT NOT NULL, canonical TEXT NOT NULL, PRIMARY KEY(installation,kind,canonical), UNIQUE(installation,kind,original));
CREATE TABLE IF NOT EXISTS bridge_variants (installation TEXT NOT NULL, product TEXT NOT NULL, variant TEXT NOT NULL, PRIMARY KEY(installation,product,variant));`;

/** Only translation state lives here. Merchant transactions stay in the application. */
export function bridgeStore({statePath, databaseUrl} = {}) {
  if (databaseUrl) {
    if (!/^postgres(?:ql)?:\/\//.test(databaseUrl)) throw Error('PostgreSQL state URL required');
    const pool = new Pool({connectionString:databaseUrl,max:4,connectionTimeoutMillis:10000});
    pool.on('error',()=>{}); // A failed connection is reported by the next operation, without exposing its DSN.
    const ready = (async()=>{
      const client=await pool.connect();
      try {
        await client.query('BEGIN');
        await client.query("SELECT pg_advisory_xact_lock(hashtext('auteric-bridge-schema-v1'))");
        await client.query(ddl); await client.query('COMMIT');
      } catch(error) { await client.query('ROLLBACK'); throw error; }
      finally { client.release(); }
    })();
    ready.catch(()=>{});
    return {
      async query(sql,parameters=[]) {
        await ready;
        let i=0; sql=sql.replace(/\?/g,()=>`$${++i}`);
        const result=await pool.query(sql,parameters);
        return {rows:result.rows,changes:result.rowCount};
      },
      close:()=>pool.end(),
    };
  }
  if (!statePath) throw Error('durable bridge state is required');
  mkdirSync(dirname(statePath),{recursive:true,mode:0o700});
  const db=new DatabaseSync(statePath); chmodSync(statePath,0o600);
  db.exec('PRAGMA busy_timeout=5000;'); db.exec(ddl);
  return {
    async query(sql,parameters=[]) {
      const stmt=db.prepare(sql);
      if (/^\s*SELECT/i.test(sql)) return {rows:stmt.all(...parameters)};
      return {rows:[],changes:Number(stmt.run(...parameters).changes)};
    },
    close:()=>db.close(),
  };
}
