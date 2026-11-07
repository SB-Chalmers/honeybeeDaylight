"""Load test: a class of students using the API, with an HTML dashboard report.

Each simulated student sends a request with a randomised building, waits for
the answer, thinks for a few seconds, and repeats. Two modes:

  loop   (default) students work at their own pace for --minutes.
  burst  (--burst N) the whole class clicks Run at the same moment, N times.

Usage:

  1. Put real EPSM construction names in constructions.json:
     {"wall": "...", "floor": "...", "roof": "...", "window": "..."}
     (not needed with --path /v1/daylight).
  2. Run a class of 30 working for 20 minutes:
     python scripts/stress_test.py --url http://<host>:8000 --key <key> \
         --students 30 --minutes 20 --constructions constructions.json
  3. Or the whole class clicking Run together, three times:
     python scripts/stress_test.py --url http://<host>:8000 --key <key> \
         --students 30 --burst 3 --constructions constructions.json
  4. Open report.html (--report to change the name). It shows latency over
     time, requests in flight, waiting versus computing, and every error.
"""

from __future__ import annotations

import argparse
import html
import json
import random
import statistics
import threading
import time
from collections import Counter
from datetime import datetime

import httpx

from client import post


def random_building(rng: random.Random, constructions: dict | None) -> dict:
    width, depth = rng.uniform(6, 30), rng.uniform(6, 20)
    payload = {
        'room': {
            'footprint_coordinates': [[0, 0], [width, 0], [width, depth], [0, depth]],
            'floors': rng.randint(1, 5),
            'orientation_offset': rng.uniform(0, 360),
            'wwr': round(rng.uniform(0.15, 0.7), 2),
        },
        'daylight': {'run_sda': rng.random() < 0.5},
    }
    if constructions:
        payload['energy'] = {'constructions': constructions,
                             'building_program': rng.choice(['Office', 'Residential', 'School'])}
    return payload


def compute_seconds(response: httpx.Response) -> float | None:
    """Time the workers spent simulating (the rest of the latency is queueing)."""
    if response.status_code != 200:
        return None
    data = response.json()
    parts = [data[k]['seconds'] for k in ('daylight', 'energy') if k in data] or [data['seconds']]
    return max(parts)


def student(index, args, constructions, t0, stop_at, barrier, records, lock):
    rng = random.Random(index)
    rounds = args.burst or float('inf')
    done = 0
    with httpx.Client(base_url=args.url, timeout=1800) as client:
        while done < rounds and (args.burst or time.time() < stop_at):
            if barrier:
                barrier.wait()  # everyone clicks Run together
            started = time.perf_counter()
            try:
                response = post(client, args.path, random_building(rng, constructions), args.key)
                status, compute = response.status_code, compute_seconds(response)
                error = '' if status == 200 else response.text[:200]
            except httpx.HTTPError as exc:
                status, compute, error = type(exc).__name__, None, str(exc)[:200]
            elapsed = time.perf_counter() - started
            with lock:
                records.append({'student': index, 'start': started - t0, 'latency': elapsed,
                                'status': status, 'compute': compute, 'error': error})
                print(f'student {index:2d}: {status} in {elapsed:6.1f}s', flush=True)
            done += 1
            if not args.burst:
                time.sleep(rng.uniform(args.think_min, args.think_max))


# ── Report ───────────────────────────────────────────────────────────────────

W, H, PAD = 720, 240, 44  # chart size and margin, px


def _scale(values, top):
    """Map 0..max(values) onto 0..top, with a round-ish upper bound."""
    high = max(values, default=1) or 1
    return high * 1.05, lambda v: v / (high * 1.05) * top


def _axes(x_max, y_max, x_label, y_label):
    """Recessive axes with three ticks each."""
    out = [f'<line class="axis" x1="{PAD}" y1="{H - PAD}" x2="{W - 10}" y2="{H - PAD}"/>']
    for i in range(4):
        y = H - PAD - i * (H - 2 * PAD) / 3
        x = PAD + i * (W - PAD - 10) / 3
        out.append(f'<line class="grid" x1="{PAD}" y1="{y:.1f}" x2="{W - 10}" y2="{y:.1f}"/>')
        out.append(f'<text x="{PAD - 6}" y="{y + 4:.1f}" text-anchor="end">{y_max * i / 3:.0f}</text>')
        out.append(f'<text x="{x:.1f}" y="{H - PAD + 16}" text-anchor="middle">{x_max * i / 3:.0f}</text>')
    out.append(f'<text x="{W - 10}" y="{H - 8}" text-anchor="end">{x_label}</text>')
    out.append(f'<text x="{PAD}" y="14">{y_label}</text>')
    return out


def latency_chart(records, end):
    """Each request as a dot: when it was sent (x) and how long it took (y)."""
    x_max, sx = _scale([end], W - PAD - 10)
    y_max, sy = _scale([r['latency'] for r in records], H - 2 * PAD)
    dots = []
    for r in sorted(records, key=lambda r: r['status'] != 200):  # errors on top
        cls = 'ok' if r['status'] == 200 else 'err'
        tip = f"student {r['student']} · {r['status']} · {r['latency']:.1f} s at {r['start']:.0f} s"
        dots.append(f'<circle class="{cls}" cx="{PAD + sx(r["start"]):.1f}" '
                    f'cy="{H - PAD - sy(r["latency"]):.1f}" r="4"><title>{html.escape(tip)}</title></circle>')
    return _svg(_axes(x_max, y_max, 'seconds into the test', 'latency, s') + dots)


def in_flight_chart(records, end):
    """Requests waiting for an answer at each moment: the load the class puts on the API."""
    events = sorted([(r['start'], 1) for r in records] + [(r['start'] + r['latency'], -1) for r in records])
    steps, level = [(0.0, 0)], 0
    for t, change in events:
        steps += [(t, level), (t, level + change)]
        level += change
    x_max, sx = _scale([end], W - PAD - 10)
    y_max, sy = _scale([n for _, n in steps], H - 2 * PAD)
    path = ' '.join(f'{PAD + sx(t):.1f},{H - PAD - sy(n):.1f}' for t, n in steps)
    peak = max(n for _, n in steps)
    return _svg(_axes(x_max, y_max, 'seconds into the test', 'requests in flight')
                + [f'<polyline class="line" points="{path}"><title>peak {peak} in flight</title></polyline>'])


def _svg(parts):
    return f'<svg viewBox="0 0 {W} {H}" role="img">{"".join(parts)}</svg>'


def write_report(path, args, records, wall_seconds):
    ok = sorted(r['latency'] for r in records if r['status'] == 200)
    compute = [r['compute'] for r in records if r['compute'] is not None]
    wait = [r['latency'] - r['compute'] for r in records if r['compute'] is not None]

    def stat(values, fn):
        return f'{fn(values):.1f} s' if values else '–'

    p95 = lambda v: v[min(len(v) - 1, int(len(v) * 0.95))]
    tiles = [
        ('Requests', len(records)),
        ('Succeeded', f'{100 * len(ok) / max(len(records), 1):.0f} %'),
        ('Median latency', stat(ok, statistics.median)),
        ('p95 latency', stat(ok, p95)),
        ('Slowest', stat(ok, max)),
        ('Median queue wait', stat(wait, statistics.median)),
        ('Median computing', stat(compute, statistics.median)),
        ('Throughput', f'{len(ok) / (wall_seconds / 60):.1f} /min'),
    ]
    end = max((r['start'] + r['latency'] for r in records), default=1)
    statuses = Counter(str(r['status']) for r in records)
    errors = [r for r in records if r['status'] != 200]
    mode = f'burst × {args.burst}' if args.burst else f'loop for {args.minutes:g} min'
    rows = ''.join(f'<tr><td>{r["student"]}</td><td>{r["start"]:.0f}</td><td>{html.escape(str(r["status"]))}</td>'
                   f'<td>{html.escape(r["error"])}</td></tr>' for r in errors[:200])
    html_doc = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Stress test report</title>
<style>
:root {{ --bg:#fcfcfb; --ink:#0b0b0b; --muted:#52514e; --grid:#e4e3df; --ok:#2a78d6; --err:#d03b3b; --card:#f4f3f0; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#1a1a19; --ink:#f0efec; --muted:#c3c2b7; --grid:#383835; --ok:#3987e5; --err:#d03b3b; --card:#242423; }} }}
body {{ margin:0 auto; max-width:760px; padding:24px 16px; background:var(--bg); color:var(--ink); font:15px/1.5 system-ui, sans-serif; }}
h1 {{ font-size:22px; margin:0 }} h2 {{ font-size:16px; margin:28px 0 4px }} .muted {{ color:var(--muted) }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(160px, 1fr)); gap:8px; margin-top:16px }}
.tile {{ background:var(--card); border-radius:8px; padding:10px 12px }} .tile b {{ display:block; font-size:22px }}
svg {{ width:100%; height:auto }} svg text {{ fill:var(--muted); font-size:11px }}
.axis {{ stroke:var(--muted) }} .grid {{ stroke:var(--grid) }} .line {{ fill:none; stroke:var(--ok); stroke-width:2 }}
.ok {{ fill:var(--ok); stroke:var(--bg); stroke-width:1.5 }} .err {{ fill:var(--err); stroke:var(--bg); stroke-width:1.5 }}
.key span {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin:0 4px 0 12px }}
table {{ border-collapse:collapse; width:100% }} td, th {{ text-align:left; padding:4px 8px; border-bottom:1px solid var(--grid); vertical-align:top }}
</style></head><body>
<h1>Stress test report</h1>
<div class="muted">{html.escape(args.url)}{html.escape(args.path)} · {args.students} students · {mode} ·
{datetime.now().astimezone():%Y-%m-%d %H:%M} · {wall_seconds / 60:.1f} min</div>
<div class="tiles">{''.join(f'<div class="tile"><span class="muted">{k}</span><b>{v}</b></div>' for k, v in tiles)}</div>
<h2>Latency of every request</h2>
<div class="muted key">Hover a dot for details.<span style="background:var(--ok)"></span>succeeded<span style="background:var(--err)"></span>failed</div>
{latency_chart(records, end)}
<h2>Requests in flight</h2>
<div class="muted">How many students were waiting for an answer at each moment.</div>
{in_flight_chart(records, end)}
<h2>Status codes</h2>
<table><tr><th>Status</th><th>Count</th></tr>{''.join(f'<tr><td>{html.escape(s)}</td><td>{n}</td></tr>' for s, n in statuses.most_common())}</table>
{f'<h2>Errors</h2><table><tr><th>Student</th><th>At, s</th><th>Status</th><th>Message</th></tr>{rows}</table>' if errors else ''}
</body></html>"""
    with open(path, 'w', encoding='utf-8') as stream:
        stream.write(html_doc)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--key', required=True)
    parser.add_argument('--path', default='/v1/simulate')
    parser.add_argument('--students', type=int, default=20)
    parser.add_argument('--minutes', type=float, default=20)
    parser.add_argument('--burst', type=int, default=0, help='rounds where everyone sends at once')
    parser.add_argument('--think-min', type=float, default=5)
    parser.add_argument('--think-max', type=float, default=30)
    parser.add_argument('--constructions', help='JSON file with EPSM construction names')
    parser.add_argument('--report', default='report.html')
    args = parser.parse_args()

    constructions = json.load(open(args.constructions)) if args.constructions else None
    if args.path != '/v1/daylight' and not constructions:
        parser.error('--constructions is required for energy requests')

    records, lock = [], threading.Lock()
    barrier = threading.Barrier(args.students) if args.burst else None
    t0 = time.perf_counter()
    stop_at = time.time() + args.minutes * 60
    threads = [threading.Thread(target=student,
                                args=(i, args, constructions, t0, stop_at, barrier, records, lock))
               for i in range(args.students)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall_seconds = time.perf_counter() - t0

    ok = sorted(r['latency'] for r in records if r['status'] == 200)
    print(f'\n{len(records)} requests, status counts: {dict(Counter(r["status"] for r in records))}')
    if ok:
        print(f'latency s  median {statistics.median(ok):.1f}  max {ok[-1]:.1f}')
    write_report(args.report, args, records, wall_seconds)
    print(f'report: {args.report}')


if __name__ == '__main__':
    main()
