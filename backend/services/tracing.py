"""Request tracing for the RAG pipeline (observability without extra infrastructure).

Every /chat request gets a trace id and a list of spans (rewrite, embed, cache, search, rerank, llm incl. time to
first token, eval). Spans are stored in Postgres (`rag_traces`) and shown as a waterfall in Admin -> Traces.
Only stage names, timings, counts, model names and document ids are recorded: no question text, no answers, no
emails (security layer 4 applies to traces too).

OpenTelemetry (optional): if `opentelemetry-sdk` + `opentelemetry-exporter-otlp` are installed and
OTEL_EXPORTER_OTLP_ENDPOINT is set (e.g. http://localhost:4318 for Jaeger / Aspire / Grafana Tempo), the same spans
are exported as a real OTel trace (root span "rag.chat" + one child span per stage).
"""
import json
import os
import time
import uuid
from contextlib import contextmanager

import psycopg2
from flask import g, has_app_context

from config import DATABASE_URL

TRACE_RETENTION = int(os.environ.get('TRACE_RETENTION', '2000'))  # newest N traces kept


@contextmanager
def _cursor():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            yield cur
    finally:
        conn.close()


def init_trace_table():
    with _cursor() as cur:
        cur.execute('''CREATE TABLE IF NOT EXISTS rag_traces (
                           id VARCHAR(32) PRIMARY KEY,
                           created_at TIMESTAMPTZ DEFAULT NOW(),
                           route VARCHAR(40), decision VARCHAR(30), role VARCHAR(20), model VARCHAR(80),
                           status INTEGER, duration_ms INTEGER, spans JSONB)''')
        cur.execute('CREATE INDEX IF NOT EXISTS rag_traces_created ON rag_traces (created_at DESC)')


# --------------------------------------------------------------------------- recording
def start_trace(route):
    g.trace = {'id': uuid.uuid4().hex[:16], 'route': route, 't0': time.time(), 'spans': []}
    return g.trace['id']


def _trace():
    return getattr(g, 'trace', None) if has_app_context() else None


def add_span(name, start, end=None, **attrs):
    """Record a finished stage: `start`/`end` are time.time() values."""
    tr = _trace()
    if tr is None:
        return
    end = end or time.time()
    tr['spans'].append({'name': name, 'start_ms': round((start - tr['t0']) * 1000, 1),
                        'duration_ms': round((end - start) * 1000, 1),
                        'attrs': {k: v for k, v in attrs.items() if v is not None}})


@contextmanager
def span(name, **attrs):
    start = time.time()
    extra = {}
    try:
        yield extra
    except Exception as e:
        extra['error'] = type(e).__name__
        raise
    finally:
        add_span(name, start, **attrs, **extra)


def finish_trace(status, decision=None, role=None, model=None):
    tr = _trace()
    if tr is None:
        return None
    duration_ms = int((time.time() - tr['t0']) * 1000)
    try:
        with _cursor() as cur:
            cur.execute('''INSERT INTO rag_traces (id, route, decision, role, model, status, duration_ms, spans)
                           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                        (tr['id'], tr['route'], decision, role, (model or '')[:80], status, duration_ms, json.dumps(tr['spans'])))
            cur.execute('DELETE FROM rag_traces WHERE id IN (SELECT id FROM rag_traces ORDER BY created_at DESC OFFSET %s)',
                        (TRACE_RETENTION,))
    except Exception as e:
        print(f"⚠️ [Trace] could not store trace: {e}")
    _export_otel(tr, duration_ms, status, decision, role, model)
    return tr['id']


# --------------------------------------------------------------------------- reading (admin)
def list_traces(limit=50):
    with _cursor() as cur:
        cur.execute('''SELECT id, created_at, route, decision, role, model, status, duration_ms, spans
                       FROM rag_traces ORDER BY created_at DESC LIMIT %s''', (limit,))
        rows = cur.fetchall()
    out = []
    for r in rows:
        spans = r[8] if isinstance(r[8], list) else json.loads(r[8] or '[]')
        out.append({'id': r[0], 'created_at': r[1].isoformat() if r[1] else None, 'route': r[2], 'decision': r[3],
                    'role': r[4], 'model': r[5], 'status': r[6], 'duration_ms': r[7], 'spans': spans})
    return out


def stage_percentiles(traces):
    """p50/p95 per stage over the given traces: where does the time actually go?"""
    by_stage = {}
    for t in traces:
        for s in t['spans']:
            by_stage.setdefault(s['name'], []).append(s['duration_ms'])
    def pct(vals, p):
        vals = sorted(vals)
        return round(vals[min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))], 1)
    return {k: {'count': len(v), 'p50_ms': pct(v, 50), 'p95_ms': pct(v, 95)} for k, v in sorted(by_stage.items())}


# --------------------------------------------------------------------------- optional OpenTelemetry export
_OTEL = None


def _otel_tracer():
    global _OTEL
    if _OTEL is not None:
        return _OTEL or None
    _OTEL = False
    endpoint = os.environ.get('OTEL_EXPORTER_OTLP_ENDPOINT', '').strip()
    if not endpoint:
        return None
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        provider = TracerProvider(resource=Resource.create({'service.name': os.environ.get('OTEL_SERVICE_NAME', 'normbridge-api')}))
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip('/') + '/v1/traces')))
        trace.set_tracer_provider(provider)
        _OTEL = trace.get_tracer('normbridge.rag')
        print(f"📡 OpenTelemetry export enabled -> {endpoint}")
    except Exception as e:
        print(f"⚠️ OpenTelemetry not available ({e}); traces are stored locally only")
    return _OTEL or None


def _export_otel(tr, duration_ms, status, decision, role, model):
    tracer = _otel_tracer()
    if not tracer:
        return
    try:
        from opentelemetry import trace as ot
        t0 = int(tr['t0'] * 1e9)
        root = tracer.start_span('rag.chat', start_time=t0, attributes={
            'rag.trace_id': tr['id'], 'rag.decision': decision or '', 'rag.role': role or '', 'rag.model': model or '',
            'http.status_code': status})
        ctx = ot.set_span_in_context(root)
        for s in tr['spans']:
            start = t0 + int(s['start_ms'] * 1e6)
            child = tracer.start_span(f"rag.{s['name']}", context=ctx, start_time=start,
                                      attributes={f'rag.{k}': (v if isinstance(v, (str, int, float, bool)) else str(v))
                                                  for k, v in s['attrs'].items()})
            child.end(end_time=start + int(s['duration_ms'] * 1e6))
        root.end(end_time=t0 + duration_ms * 1_000_000)
    except Exception as e:
        print(f"⚠️ [Trace] OTel export failed: {e}")
