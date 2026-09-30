"""Single-node durable control-plane storage; explicit transactions and tenant scopes."""

import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

request_trace = ContextVar('gateway_request_trace', default=None)


def uid():
    return uuid.uuid4().hex


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


class HybridRow:
    """Small sqlite.Row-compatible result used by the PostgreSQL backend."""

    def __init__(self, columns, values):
        self._values = tuple(values)
        self._data = dict(zip(columns, self._values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._data[key]

    def __iter__(self):
        return iter(self._data)

    def keys(self):
        return self._data.keys()


class PostgresCursor:
    def __init__(self, cursor):
        self.cursor = cursor

    @property
    def rowcount(self):
        return self.cursor.rowcount

    def _row(self, value):
        if value is None:
            return None
        columns = [item.name for item in self.cursor.description]
        return HybridRow(columns, value)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        while True:
            row = self.fetchone()
            if row is None:
                return
            yield row


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    @staticmethod
    def _sql(sql):
        if sql.strip().upper() == 'BEGIN IMMEDIATE':
            return 'BEGIN'
        sql = sql.replace('? IS NULL', 'CAST(? AS TEXT) IS NULL')
        return sql.replace('?', '%s')

    def execute(self, sql, parameters=()):
        if isinstance(parameters, dict):
            sql = re.sub(r'(?<!:):([a-z_]+)', r'%(\1)s', sql)
        return PostgresCursor(self.connection.execute(self._sql(sql), parameters))

    def executemany(self, sql, parameters):
        cursor = self.connection.cursor()
        cursor.executemany(self._sql(sql), parameters)
        return PostgresCursor(cursor)

    def executescript(self, script):
        script = script.replace('PRAGMA journal_mode=WAL;', '')
        script = script.replace('INTEGER PRIMARY KEY AUTOINCREMENT', 'BIGSERIAL PRIMARY KEY')
        # PostgreSQL REAL is float32 and cannot represent epoch timestamps with
        # the precision required for expiries, heartbeats and approval ordering.
        script = re.sub(r'\bREAL\b', 'DOUBLE PRECISION', script)
        # SQLite trigger syntax is replaced by one PostgreSQL trigger below.
        marker = 'CREATE TRIGGER IF NOT EXISTS audit_no_update'
        if marker in script:
            script = script[:script.index(marker)]
        for statement in script.split(';'):
            if statement.strip():
                self.connection.execute(statement)
        self.connection.execute("""
            CREATE OR REPLACE FUNCTION auteric_audit_append_only() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'append-only'; END; $$ LANGUAGE plpgsql
        """)
        self.connection.execute('DROP TRIGGER IF EXISTS audit_no_update ON audit')
        self.connection.execute('DROP TRIGGER IF EXISTS audit_no_delete ON audit')
        self.connection.execute(
            'CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit FOR EACH ROW EXECUTE FUNCTION auteric_audit_append_only()'
        )
        self.connection.execute(
            'CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit FOR EACH ROW EXECUTE FUNCTION auteric_audit_append_only()'
        )


class Store:
    def __init__(self, path):
        self.database_url = str(path)
        self.postgres = self.database_url.startswith(('postgresql://', 'postgres://'))
        self.path = str(Path('.runtime/commerce/control.db')) if self.postgres else self.database_url
        self.integrity_errors = (sqlite3.IntegrityError,)
        self.pool = None
        if self.postgres:
            try:
                import psycopg
                from psycopg_pool import ConnectionPool
            except ImportError as exc:
                raise RuntimeError('PostgreSQL DATABASE_URL requires psycopg[binary] and psycopg-pool') from exc
            self.integrity_errors = (sqlite3.IntegrityError, psycopg.IntegrityError)
            # The Console's read views intentionally make several small,
            # tenant-scoped queries. Reusing a bounded pool avoids paying a
            # PostgreSQL TLS/authentication handshake for each one, without
            # changing transaction or ownership boundaries.
            self.pool = ConnectionPool(
                conninfo=self.database_url,
                min_size=1,
                max_size=6,
                timeout=10,
                # A long-lived ECS task can retain a socket after an RDS TLS
                # connection is closed. Validate it before handing it to a
                # request so rate limiting/authentication never turns that
                # transient condition into a customer-facing 500.
                check=ConnectionPool.check_connection,
                kwargs={'connect_timeout': 10},
            )
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.runtime_target = self if self.postgres else self.path + '.runtime'
        with self.db() as db:
            db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE,password TEXT,org TEXT,name TEXT);
            CREATE TABLE IF NOT EXISTS google_identities(subject TEXT PRIMARY KEY,user_id TEXT NOT NULL,
              email TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS cli_authorizations(id TEXT PRIMARY KEY,challenge TEXT NOT NULL,
              state_hash TEXT NOT NULL,user_id TEXT,exchange_hash TEXT,expires REAL NOT NULL,
              consumed REAL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS stores(id TEXT PRIMARY KEY,org TEXT,name TEXT,domain TEXT,environment TEXT,
              verification_token TEXT,verified REAL,heartbeat REAL,connector_version TEXT,policy TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS credentials(hash TEXT PRIMARY KEY,store TEXT,kind TEXT,
              created REAL,revoked REAL);
            CREATE TABLE IF NOT EXISTS mappings(id TEXT PRIMARY KEY,store TEXT,operation TEXT,body TEXT,state TEXT,
              fingerprint TEXT,tests TEXT,created REAL,activated REAL);
            CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY,store TEXT,operation TEXT,input TEXT,mapping TEXT,
              mapping_version TEXT,state TEXT,result TEXT,error TEXT,created REAL,claimed REAL,expires REAL);
            CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY AUTOINCREMENT,org TEXT,store TEXT,
              actor TEXT,event TEXT,data TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS traffic(id TEXT PRIMARY KEY,store TEXT,agent TEXT,session TEXT,operation TEXT,
              input TEXT,decision TEXT,state TEXT,response TEXT,error TEXT,latency REAL,
              created REAL,mapping_version TEXT);
            CREATE TABLE IF NOT EXISTS resources(store TEXT,kind TEXT,id TEXT,agent TEXT,session TEXT,
              PRIMARY KEY(store,kind,id));
            CREATE TABLE IF NOT EXISTS resource_locks(store TEXT,resource TEXT,action TEXT,
              PRIMARY KEY(store,resource));
            CREATE TABLE IF NOT EXISTS counters(bucket TEXT PRIMARY KEY,count INTEGER,expires REAL);
            CREATE TABLE IF NOT EXISTS agent_sessions(hash TEXT PRIMARY KEY,store TEXT,agent TEXT,expires REAL);
            CREATE TABLE IF NOT EXISTS setup_authorizations(hash TEXT PRIMARY KEY,store TEXT,org TEXT,
              expires REAL,completed REAL,created REAL);
            CREATE TABLE IF NOT EXISTS capability_controls(store TEXT,operation TEXT,enabled INTEGER,updated REAL,
              PRIMARY KEY(store,operation));
            CREATE TABLE IF NOT EXISTS mcp_grants(credential TEXT PRIMARY KEY,store TEXT NOT NULL,
              scopes TEXT NOT NULL,created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ucp_config(store TEXT PRIMARY KEY,payment_handlers TEXT NOT NULL,
              identity_linking TEXT,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS routing_bindings(hostname TEXT PRIMARY KEY,store TEXT,method TEXT,
              verified REAL,created REAL);
            CREATE TABLE IF NOT EXISTS mapping_promotions(id TEXT PRIMARY KEY,store TEXT,mapping TEXT,
              source_store TEXT,source_mapping TEXT,release_digest TEXT,evidence TEXT,reviewer TEXT,created REAL);
            CREATE TABLE IF NOT EXISTS connection_runs(id TEXT PRIMARY KEY,store TEXT,actor TEXT,
              binding TEXT,state TEXT,body TEXT,created REAL,updated REAL);
            CREATE TABLE IF NOT EXISTS onboarding_runs(id TEXT PRIMARY KEY,store TEXT NOT NULL,actor TEXT NOT NULL,
              request_key TEXT NOT NULL,state TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL,updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS onboarding_run_requests(store TEXT NOT NULL,request_key TEXT NOT NULL,
              run_id TEXT NOT NULL,created REAL NOT NULL,PRIMARY KEY(store,request_key));
            CREATE TABLE IF NOT EXISTS gateway_controls(id TEXT PRIMARY KEY,enabled INTEGER NOT NULL,updated REAL);
            CREATE TABLE IF NOT EXISTS gateway_requests(id TEXT PRIMARY KEY,store TEXT,route TEXT,
              method TEXT,status INTEGER,latency REAL,created REAL);
            CREATE TABLE IF NOT EXISTS shopify_cart_aliases(store TEXT,shop TEXT,alias TEXT,remote TEXT,created REAL,
              PRIMARY KEY(store,alias), UNIQUE(store,shop,remote));
            CREATE TABLE IF NOT EXISTS installations(id TEXT PRIMARY KEY,store_id TEXT NOT NULL,
              environment TEXT NOT NULL,transport TEXT NOT NULL,endpoint TEXT NOT NULL,
              trust_binding TEXT NOT NULL,protocol_version TEXT NOT NULL,sdk_version TEXT,
              release_id TEXT,revoked_at REAL,created_at REAL NOT NULL,updated_at REAL NOT NULL,
              UNIQUE(store_id,environment));
            CREATE TABLE IF NOT EXISTS installation_operations(installation_id TEXT NOT NULL,
              operation TEXT NOT NULL,contract_digest TEXT NOT NULL,binding_digest TEXT NOT NULL,
              test_evidence TEXT,deployment_evidence TEXT,UNIQUE(installation_id,operation));
            CREATE TABLE IF NOT EXISTS capability_policies(store_id TEXT NOT NULL,operation TEXT NOT NULL,
              enabled INTEGER NOT NULL,revision INTEGER NOT NULL,actor TEXT,updated_at REAL NOT NULL,
              UNIQUE(store_id,operation));
            CREATE TABLE IF NOT EXISTS execution_actions(store_id TEXT NOT NULL,installation_id TEXT NOT NULL,
              principal TEXT,operation TEXT NOT NULL,action_id TEXT NOT NULL,request_hash TEXT NOT NULL,
              outcome TEXT NOT NULL,correlation_id TEXT,created_at REAL NOT NULL,completed_at REAL,
              retention_until REAL,
              UNIQUE(installation_id,action_id));
            CREATE TABLE IF NOT EXISTS execution_receipts(installation_id TEXT NOT NULL,
              action_id TEXT NOT NULL,store_id TEXT NOT NULL,principal TEXT NOT NULL,
              operation TEXT NOT NULL,request_hash TEXT NOT NULL,result TEXT NOT NULL,
              created_at REAL NOT NULL,PRIMARY KEY(installation_id,action_id));
            CREATE TABLE IF NOT EXISTS sidecar_test_scopes(token_hash TEXT PRIMARY KEY,store_id TEXT NOT NULL,
                installation_id TEXT NOT NULL,actor TEXT NOT NULL,run_id TEXT NOT NULL,operation TEXT NOT NULL,
                input_hash TEXT NOT NULL,request_key TEXT NOT NULL,expires_at REAL NOT NULL,binding TEXT);
            CREATE TABLE IF NOT EXISTS sidecar_grants(action_id TEXT PRIMARY KEY,store_id TEXT NOT NULL,
              installation_id TEXT NOT NULL,agent TEXT NOT NULL,session TEXT NOT NULL,
              envelope TEXT NOT NULL,expires_at REAL NOT NULL,state TEXT NOT NULL,
              result TEXT,error TEXT,created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS principal_bindings(installation_id TEXT NOT NULL,
              external_subject TEXT NOT NULL,merchant_principal TEXT NOT NULL,kind TEXT NOT NULL,
              scopes TEXT NOT NULL,consent_reference TEXT,created_at REAL NOT NULL,revoked_at REAL,
              UNIQUE(installation_id,external_subject));
            CREATE TABLE IF NOT EXISTS resource_owners(installation_id TEXT NOT NULL,
              resource_type TEXT NOT NULL,resource_id TEXT NOT NULL,principal_id TEXT NOT NULL,
              created_at REAL NOT NULL,UNIQUE(installation_id,resource_type,resource_id));
            CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit
              BEGIN SELECT RAISE(ABORT,'append-only'); END;
            CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit
              BEGIN SELECT RAISE(ABORT,'append-only'); END;
            """)
            # Additive migrations keep existing pilot databases usable. Store remains
            # the commerce ownership unit; no separate Installation is introduced.
            if self.postgres:
                columns = {row[0] for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='stores'"
                )}
            else:
                columns = {row[1] for row in db.execute("PRAGMA table_info(stores)")}
            additions = {
                "platform": "TEXT NOT NULL DEFAULT 'custom'",
                "integration_mode": "TEXT NOT NULL DEFAULT 'enable_protect'",
                "agent_access_enabled": "INTEGER NOT NULL DEFAULT 0",
                "verification_method": "TEXT",
                "connector_release": "TEXT",
                "policy_reviewed_at": "REAL",
                "detached_at": "REAL",
                "detached_by": "TEXT",
            }
            for name, definition in additions.items():
                if name not in columns:
                    if self.postgres:
                        definition = definition.replace('REAL', 'DOUBLE PRECISION')
                    db.execute(f"ALTER TABLE stores ADD COLUMN {name} {definition}")
            if self.postgres:
                cli_columns = {row[0] for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='cli_authorizations'"
                )}
            else:
                cli_columns = {row[1] for row in db.execute("PRAGMA table_info(cli_authorizations)")}
            if "completed_store" not in cli_columns:
                db.execute("ALTER TABLE cli_authorizations ADD COLUMN completed_store TEXT")
            if self.postgres:
                action_columns = {row[0] for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='execution_actions'"
                )}
            else:
                action_columns = {row[1] for row in db.execute("PRAGMA table_info(execution_actions)")}
            if self.postgres:
                scope_columns = {row[0] for row in db.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='sidecar_test_scopes'"
                )}
            else:
                scope_columns = {row[1] for row in db.execute("PRAGMA table_info(sidecar_test_scopes)")}
            if "binding" not in scope_columns:
                db.execute("ALTER TABLE sidecar_test_scopes ADD COLUMN binding TEXT")
            if "retention_until" not in action_columns:
                db.execute("ALTER TABLE execution_actions ADD COLUMN retention_until REAL")
            if self.postgres:
                managed = {'users', 'sessions', 'cli_authorizations', 'stores', 'credentials', 'mappings', 'jobs', 'audit',
                           'traffic', 'counters', 'agent_sessions', 'setup_authorizations', 'capability_controls', 'ucp_config',
                           'routing_bindings', 'mapping_promotions', 'connection_runs', 'gateway_controls',
                           'gateway_requests', 'shopify_cart_aliases', 'onboarding_runs', 'onboarding_run_requests'}
                legacy = db.execute("SELECT table_name,column_name FROM information_schema.columns "
                                    "WHERE table_schema=current_schema() AND data_type='real'").fetchall()
                for column in legacy:
                    table, name = column['table_name'], column['column_name']
                    if table in managed and re.fullmatch(r'[a-z_]+', name):
                        db.execute(f'ALTER TABLE {table} ALTER COLUMN {name} TYPE DOUBLE PRECISION')
                db.execute('''CREATE TABLE IF NOT EXISTS runtime_actions (
                  merchant TEXT NOT NULL, id TEXT NOT NULL, request_key TEXT NOT NULL,
                  request TEXT NOT NULL, action TEXT NOT NULL, decision TEXT NOT NULL,
                  binding TEXT NOT NULL, policy TEXT NOT NULL, digest TEXT NOT NULL,
                  state TEXT NOT NULL, creator TEXT NOT NULL, approver TEXT,
                  expires DOUBLE PRECISION NOT NULL, created DOUBLE PRECISION NOT NULL,
                  PRIMARY KEY(merchant,id), UNIQUE(merchant,request_key))''')
                db.execute('''CREATE TABLE IF NOT EXISTS runtime_events (
                  sequence BIGSERIAL PRIMARY KEY, merchant TEXT NOT NULL,
                  action_id TEXT NOT NULL, trace_id TEXT NOT NULL, actor TEXT NOT NULL,
                  event TEXT NOT NULL, state TEXT NOT NULL, at DOUBLE PRECISION NOT NULL,
                  payload TEXT NOT NULL DEFAULT '{}')''')
                for mutation in ('update', 'delete'):
                    db.execute(f'DROP TRIGGER IF EXISTS runtime_events_no_{mutation} ON runtime_events')
                    db.execute(f'CREATE TRIGGER runtime_events_no_{mutation} BEFORE {mutation.upper()} ON runtime_events '
                               'FOR EACH ROW EXECUTE FUNCTION auteric_audit_append_only()')

    @contextmanager
    def db(self):
        if self.postgres:
            with self.pool.connection() as raw:
                db = PostgresConnection(raw)
                try:
                    yield db
                    raw.commit()
                except Exception:
                    # A broken PostgreSQL socket can fail again on rollback.
                    # Preserve the original application/database exception;
                    # the pool will discard the failed connection on exit.
                    try:
                        raw.rollback()
                    except Exception:
                        pass
                    raise
            return
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def close(self):
        if self.pool is not None:
            self.pool.close()

    def event(self, org, store, actor, event, data=None):
        data = {**(data or {}), **({'trace_id': request_trace.get()} if request_trace.get() else {})}
        with self.db() as db:
            db.execute(
                "INSERT INTO audit(org,store,actor,event,data,created) VALUES(?,?,?,?,?,?)",
                (org, store, actor, event, encode(data), time.time()),
            )

    def limit(self, key, maximum, seconds=60):
        now = time.time()
        bucket = key + ":" + str(int(now // seconds))
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM counters WHERE expires<?", (now,))
            db.execute(
                "INSERT INTO counters VALUES(?,1,?) ON CONFLICT(bucket) DO UPDATE SET count=counters.count+1",
                (bucket, now + seconds * 2),
            )
            return db.execute("SELECT count FROM counters WHERE bucket=?", (bucket,)).fetchone()[0] <= maximum
