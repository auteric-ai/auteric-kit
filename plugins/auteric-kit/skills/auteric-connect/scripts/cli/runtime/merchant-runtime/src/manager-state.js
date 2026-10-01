import { Pool } from 'pg';

/** Persist uncertainty across Fargate replacement; never use this as verification evidence. */
export function managerState(databaseUrl,installationId) {
  const pool=new Pool({connectionString:databaseUrl,max:2,connectionTimeoutMillis:10000});
  pool.on('error',()=>{});
  const ready=(async()=>{const client=await pool.connect();try{await client.query('BEGIN');await client.query("SELECT pg_advisory_xact_lock(hashtext('auteric-runtime-state-v1'))");await client.query('CREATE TABLE IF NOT EXISTS auteric_runtime_state (installation TEXT PRIMARY KEY, body TEXT NOT NULL)');await client.query('COMMIT');}catch(error){await client.query('ROLLBACK');throw error;}finally{client.release();}})();
  ready.catch(()=>{});
  return {
    async read(){await ready;const result=await pool.query('SELECT body FROM auteric_runtime_state WHERE installation=$1',[installationId]);return result.rows[0]?JSON.parse(result.rows[0].body):null;},
    async save(value){await ready;await pool.query('INSERT INTO auteric_runtime_state VALUES($1,$2) ON CONFLICT(installation) DO UPDATE SET body=excluded.body',[installationId,JSON.stringify(value)]);},
    close:()=>pool.end(),
  };
}
