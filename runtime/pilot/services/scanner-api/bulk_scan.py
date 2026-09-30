"""Resumable internal batch ingestion through app.run_scan, without an HTTP proxy."""
import argparse
import asyncio
import csv
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path

from models import ScanCreateRequest
from public_reports import domain_name
from readiness import VERSION
from traffic_limit import RequestLimiter, request_limiter


def load_domains(path):
    path = Path(path)
    with path.open(encoding='utf-8-sig', newline='') as f:
        if path.suffix.lower() == '.csv':
            reader = csv.DictReader(f)
            if 'domain' not in (reader.fieldnames or []):
                raise ValueError('CSV must contain a domain column')
            values = [row.get('domain') or '' for row in reader]
        else:
            values = [line.strip() for line in f if line.strip() and not line.lstrip().startswith('#')]
    domains, errors, seen = [], [], set()
    for number, value in enumerate(values, 1):
        try:
            domain = domain_name(value)
        except (ValueError, UnicodeError):
            errors.append({'row': number, 'error': 'Invalid public domain'})
            continue
        if domain not in seen:
            domains.append(domain)
            seen.add(domain)
    return domains, errors, len(values) - len(domains) - len(errors)


class BatchState:
    def __init__(self, path):
        self.path = path
        with sqlite3.connect(path) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS bulk_jobs(
                batch TEXT, domain TEXT, version TEXT, state TEXT, scan_id TEXT,
                lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(batch,domain,version))''')

    def acquire(self, batch, domain, timeout):
        scan_id = uuid.uuid4().hex
        with sqlite3.connect(self.path, timeout=10) as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('INSERT OR IGNORE INTO bulk_jobs(batch,domain,version,state) VALUES(?,?,?,?)', (batch, domain, VERSION, 'pending'))
            changed = c.execute('''UPDATE bulk_jobs SET state='running',scan_id=?,lease_until=?,attempts=attempts+1
                 WHERE batch=? AND domain=? AND version=? AND state!='completed'
                 AND (state!='running' OR lease_until<?)''',
                 (scan_id, time.time() + timeout, batch, domain, VERSION, time.time())).rowcount
        return scan_id if changed else None

    def finish(self, batch, domain, scan_id, state):
        with sqlite3.connect(self.path) as c:
            c.execute('UPDATE bulk_jobs SET state=?,lease_until=0 WHERE batch=? AND domain=? AND version=? AND scan_id=?',
                      (state, batch, domain, VERSION, scan_id))


def transient(error):
    return any(s in str(error).lower() for s in ('timeout', 'timed out', '429', '502', '503', '504', 'connection', 'temporar'))


async def run_batch(domains, *, store, reports, runner, batch, concurrency=3, interval=1.0,
                    timeout=180, retries=2, recent_hours=24, force=False, requests_per_second=5):
    jobs = BatchState(store.path)
    sem, rate = asyncio.Semaphore(concurrency), asyncio.Lock()
    next_start = 0.0
    counts = {'Total': len(domains), 'Successful': 0, 'Failed': 0, 'Skipped': 0}
    async def one(domain):
        nonlocal next_start
        async with sem:
            scan_id = None
            try:
                with sqlite3.connect(store.path) as db:
                    prior = db.execute('SELECT scan_id FROM bulk_jobs WHERE batch=? AND domain=? AND version=?', (batch, domain, VERSION)).fetchone()
                recovered = store.get(prior[0]) if prior and prior[0] else None
                if recovered and recovered.get('status') == 'completed':
                    reports.publish(recovered)
                    jobs.finish(batch, domain, prior[0], 'completed')
                    counts['Skipped'] += 1
                    return
                latest = reports.rows(domain)
                if not force and latest:
                    from datetime import datetime
                    when = datetime.fromisoformat(latest[0]['completed_at'].replace('Z', '+00:00')).timestamp()
                    if time.time() - when < recent_hours * 3600 and latest[0].get('scan_version') == VERSION:
                        counts['Skipped'] += 1
                        return
                scan_id = jobs.acquire(batch, domain, (timeout + interval * concurrency + 10) * (retries + 1) + 60)
                if scan_id is None:
                    counts['Skipped'] += 1
                    return
                target = 'https://' + domain
                store.create(scan_id, target, 'generic')
                success = False
                for attempt in range(retries + 1):
                    async with rate:
                        await asyncio.sleep(max(0, next_start - time.monotonic()))
                        next_start = time.monotonic() + interval
                    try:
                        await asyncio.wait_for(runner(scan_id, ScanCreateRequest(target_url=target)), timeout)
                    except asyncio.TimeoutError:
                        store.fail(scan_id, 'Batch scan timeout')
                    except Exception as exc:
                        store.fail(scan_id, str(exc))
                    record = store.get(scan_id) or {}
                    if record.get('status') == 'completed':
                        # Retry publication when app's best-effort publishing failed.
                        reports.publish(record)
                        success = True
                        break
                    if attempt >= retries or not transient(record.get('error')):
                        break
                    await asyncio.sleep(min(2 ** attempt, 8))
                jobs.finish(batch, domain, scan_id, 'completed' if success else 'failed')
                counts['Successful' if success else 'Failed'] += 1
            except Exception:
                if scan_id:
                    jobs.finish(batch, domain, scan_id, 'failed')
                counts['Failed'] += 1
    token = request_limiter.set(RequestLimiter(requests_per_second))
    try:
        await asyncio.gather(*(one(domain) for domain in dict.fromkeys(domains)))
    finally:
        request_limiter.reset(token)
    counts['Skipped'] += len(domains) - len(set(domains))
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('file')
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--interval', type=float, default=1, help='Minimum seconds between scan starts (not individual HTTP requests)')
    parser.add_argument('--requests-per-second', type=float, default=5, help='Global request rate across the batch, including redirects')
    parser.add_argument('--timeout', type=float, default=180)
    parser.add_argument('--retries', type=int, default=2)
    parser.add_argument('--recent-hours', type=float, default=24)
    parser.add_argument('--force', action='store_true', help='Start a new batch and ignore recent results')
    parser.add_argument('--batch-id', help='Explicit durable resume key; default is a hash of normalized domains and scan version')
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 20 or args.interval < 0 or args.timeout <= 0 or not 0 <= args.retries <= 5 or args.recent_hours < 0 or not 0 < args.requests_per_second <= 100:
        parser.error('Invalid concurrency, rate, timeout, retries or recency setting')
    domains, errors, duplicates = load_domains(args.file)
    from app import store, public_reports, run_scan
    batch = args.batch_id or (uuid.uuid4().hex if args.force else hashlib.sha256((VERSION + '\n' + '\n'.join(sorted(domains))).encode()).hexdigest())
    result = asyncio.run(run_batch(domains, store=store, reports=public_reports, runner=run_scan,
                                  batch=batch, concurrency=args.concurrency, interval=args.interval,
                                  timeout=args.timeout, retries=args.retries, recent_hours=args.recent_hours, force=args.force, requests_per_second=args.requests_per_second))
    result['Total'] += len(errors) + duplicates
    result['Failed'] += len(errors)
    result['Skipped'] += duplicates
    print('Batch: ' + batch)
    for key, value in result.items():
        print(f'{key}: {value}')
    if errors:
        print(json.dumps({'invalid_rows': errors}))
    raise SystemExit(1 if result['Failed'] else 0)


if __name__ == '__main__':
    main()
