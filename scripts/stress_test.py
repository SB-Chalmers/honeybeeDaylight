"""Load test: a class of students using the API, with an HTML dashboard report.

Each simulated student sends a request with a randomised building, waits for
the answer, thinks, and repeats. Like a real class:

  - most think 5-30 s between runs, but sometimes iterate fast (1-3 s);
  - every --burst-every seconds the teacher says "run it now" and every idle
    student clicks at once;
  - a student who waits longer than --give-up seconds gives up and clicks
    again. The abandoned job still runs on the server.

Usage:

  1. Put real EPSM construction names in constructions.json:
     {"wall": "...", "floor": "...", "roof": "...", "window": "..."}
     (not needed with --path /v1/daylight).
  2. Run a class of 30 for 10 minutes, all clicking together every 2 minutes,
     giving up after 60 s:
     python scripts/stress_test.py --url http://<host>:8000 --key <key> \
         --students 30 --minutes 10 --burst-every 120 --give-up 60 \
         --constructions constructions.json
  3. Open report.html (--report to change the name). It shows which worker
     slot ran each job, how the queues grew, and who gave up or failed.
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
from pathlib import Path

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


def jobs_of(response: httpx.Response, t0: float) -> list[dict]:
    """Where and when each part of the request ran: [{queue, worker, start, end}]."""
    if response.status_code != 200:
        return []
    data = response.json()
    parts = {k: data[k] for k in ('daylight', 'energy') if k in data}
    if not parts:
        parts = {response.request.url.path.rsplit('/', 1)[-1]: data}
    return [{'queue': queue, 'worker': part.get('worker') or '?',
             'start': part.get('started_at', 0) - t0, 'seconds': part['seconds']}
            for queue, part in parts.items()]


class Teacher:
    """Rings a bell every N seconds; idle students click Run when it rings."""

    def __init__(self):
        self.rings, self.times, self.cond = 0, [], threading.Condition()

    def ring(self, at: float):
        with self.cond:
            self.rings += 1
            self.times.append(at)
            self.cond.notify_all()

    def wait(self, seen: int, seconds: float):
        with self.cond:
            self.cond.wait_for(lambda: self.rings != seen, timeout=seconds)


def student(index, args, constructions, t0, stop_at, teacher, records, lock):
    rng = random.Random(index)
    timeout = httpx.Timeout(args.give_up or 1800, connect=10)
    with httpx.Client(base_url=args.url, timeout=timeout) as client:
        while time.time() < stop_at:
            sent = time.time()
            try:
                response = post(client, args.path, random_building(rng, constructions), args.key)
                status, parts = response.status_code, jobs_of(response, t0)
                error = '' if status == 200 else response.text[:200]
            except httpx.TimeoutException:
                status, parts, error = 'gave up', [], f'no answer after {args.give_up:g} s'
            except httpx.HTTPError as exc:
                status, parts, error = type(exc).__name__, [], str(exc)[:200]
            received = time.time()
            with lock:
                records.append({'student': index, 'sent': sent - t0, 'latency': received - sent,
                                'status': status, 'parts': parts, 'error': error})
                print(f'student {index:2d}: {status} in {received - sent:6.1f}s', flush=True)
            # Think, iterate fast now and then, retry soon after a failure,
            # and click at once when the teacher says so.
            fast = status != 200 or rng.random() < 0.25
            seen = teacher.rings
            think = rng.uniform(1, 3) if fast else rng.uniform(args.think_min, args.think_max)
            teacher.wait(seen, min(think, max(stop_at - time.time(), 0)))


# ── Report ───────────────────────────────────────────────────────────────────

W, LEFT, RIGHT, TOP, BOTTOM = 760, 84, 12, 22, 30  # chart width and margins, px


def _chart(height, x_max, y_max, y_label, bursts, body, y_ticks=True):
    """An SVG chart: recessive grid, time axis, dashed lines where the class clicked together."""
    sx = lambda t: LEFT + t / x_max * (W - LEFT - RIGHT)
    sy = lambda v: height - BOTTOM - v / y_max * (height - TOP - BOTTOM)
    out = [f'<line class="axis" x1="{LEFT}" y1="{height - BOTTOM}" x2="{W - RIGHT}" y2="{height - BOTTOM}"/>']
    for i in range(5):
        t = x_max * i / 4
        anchor = 'end' if i == 4 else 'middle'
        out.append(f'<text x="{sx(t):.1f}" y="{height - BOTTOM + 15}" text-anchor="{anchor}">{t:.0f} s</text>')
        if y_ticks:
            v = y_max * i / 4
            out.append(f'<line class="grid" x1="{LEFT}" y1="{sy(v):.1f}" x2="{W - RIGHT}" y2="{sy(v):.1f}"/>')
            out.append(f'<text x="{LEFT - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{v:.0f}</text>')
    for t in bursts:
        out.append(f'<line class="burst" x1="{sx(t):.1f}" y1="{TOP - 8}" x2="{sx(t):.1f}" y2="{height - BOTTOM}">'
                   f'<title>whole class clicked Run at {t:.0f} s</title></line>')
    out.append(f'<text x="{LEFT}" y="10">{y_label}</text>')
    out += body(sx, sy)
    return f'<svg viewBox="0 0 {W} {height}" role="img">{"".join(out)}</svg>'


def _natural(worker: str):
    """Sort "host:10" after "host:9"."""
    host, _, slot = worker.rpartition(':')
    return host, int(slot) if slot.isdigit() else 0


def worker_timeline(records, x_max, bursts):
    """One row per worker slot, one bar per job it ran (the gaps are idle time)."""
    jobs = [(r, p) for r in records for p in r['parts']]
    lanes = sorted({(p['queue'], p['worker']) for _, p in jobs}, key=lambda lane: (lane[0], _natural(lane[1])))
    if not lanes:
        return '<p class="muted">No finished jobs to show.</p>'
    hosts = {w.rsplit(':', 1)[0] for _, w in lanes}
    row, height = 18, TOP + BOTTOM + 18 * len(lanes)

    def body(sx, sy):
        out = []
        for i, (queue, worker) in enumerate(lanes):
            y = TOP + i * row
            name = ('GPU' if queue == 'daylight' else 'CPU') + ' ' + (worker if len(hosts) > 1 else worker.rsplit(':', 1)[-1])
            out.append(f'<text x="{LEFT - 6}" y="{y + 12}" text-anchor="end">{html.escape(name)}</text>')
        for r, p in jobs:
            i = lanes.index((p['queue'], p['worker']))
            # Clamp to the client's own send/receive times (clocks can differ slightly).
            start = min(max(p['start'], r['sent']), r['sent'] + r['latency'] - p['seconds'])
            tip = (f"student {r['student']} · {p['queue']} · waited {start - r['sent']:.1f} s "
                   f"· ran {p['seconds']:.1f} s")
            out.append(f'<rect class="{p["queue"]}" x="{sx(start):.1f}" y="{TOP + i * row + 2}" '
                       f'width="{max(sx(start + p["seconds"]) - sx(start) - 1, 1):.1f}" height="{row - 4}" rx="2">'
                       f'<title>{html.escape(tip)}</title></rect>')
        return out

    return _chart(height, x_max, 1, 'worker slot', bursts, body, y_ticks=False)


def queue_chart(records, x_max, bursts):
    """Jobs waiting in each queue over time."""
    series = {}
    for r in records:
        for p in r['parts']:
            start = min(max(p['start'], r['sent']), r['sent'] + r['latency'] - p['seconds'])
            series.setdefault(p['queue'], []).extend([(r['sent'], 1), (start, -1)])
    # Jobs the students gave up on: still queued or running on the server, unseen by the test.
    abandoned = [(r['sent'] + r['latency'], 1) for r in records if r['status'] == 'gave up']
    if abandoned:
        series['abandoned'] = abandoned
    lines, peak = {}, 1
    for queue, events in series.items():
        level, steps = 0, [(0.0, 0)]
        for t, change in sorted(events):
            steps += [(t, level), (t, level + change)]
            level += change
        lines[queue] = steps
        peak = max(peak, max(n for _, n in steps))

    def body(sx, sy):
        return [f'<polyline class="line {q}" points="{" ".join(f"{sx(t):.1f},{sy(n):.1f}" for t, n in steps)}">'
                f'<title>{q}: up to {max(n for _, n in steps)}</title></polyline>' for q, steps in lines.items()]

    return _chart(200, x_max, peak * 1.1, 'jobs', bursts, body)


def latency_chart(records, x_max, bursts, give_up):
    """Each request: when it was sent (x) and how long the student waited (y)."""
    y_max = max([r['latency'] for r in records] + [give_up or 0]) * 1.05 or 1

    def body(sx, sy):
        out = []
        if give_up:
            out.append(f'<line class="limit" x1="{LEFT}" y1="{sy(give_up):.1f}" x2="{W - RIGHT}" y2="{sy(give_up):.1f}">'
                       f'<title>students give up after {give_up:g} s</title></line>')
        for r in sorted(records, key=lambda r: r['status'] != 200):  # failures on top
            tip = f"student {r['student']} · {r['status']} · {r['latency']:.1f} s"
            out.append(f'<circle class="{"ok" if r["status"] == 200 else "err"}" cx="{sx(r["sent"]):.1f}" '
                       f'cy="{sy(r["latency"]):.1f}" r="4"><title>{html.escape(tip)}</title></circle>')
        return out

    return _chart(220, x_max, y_max, 'seconds the student waited', bursts, body)


def write_report(path, args, records, bursts, wall_seconds):
    ok = sorted(r['latency'] for r in records if r['status'] == 200)
    parts = [(r, p) for r in records for p in r['parts']]
    compute = [p['seconds'] for _, p in parts]
    wait = [max(p['start'] - r['sent'], 0) for r, p in parts]
    gave_up = sum(r['status'] == 'gave up' for r in records)
    x_max = max([r['sent'] + r['latency'] for r in records] + [1])

    def stat(values, fn):
        return f'{fn(values):.1f} s' if values else '–'

    p95 = lambda v: sorted(v)[min(len(v) - 1, int(len(v) * 0.95))]
    tiles = [
        ('Requests', len(records)),
        ('Succeeded', f'{100 * len(ok) / max(len(records), 1):.0f} %'),
        ('Gave up', gave_up),
        ('Throughput', f'{len(ok) / (wall_seconds / 60):.1f} /min'),
        ('Median latency', stat(ok, statistics.median)),
        ('p95 latency', stat(ok, p95)),
        ('Median queue wait', stat(wait, statistics.median)),
        ('Median compute', stat(compute, statistics.median)),
    ]
    failures = {}
    for r in records:
        if r['status'] != 200:
            failures.setdefault((r['status'], r['error']), []).append(r['sent'])
    settings = (f'{args.url}{args.path} · {args.students} students · {wall_seconds / 60:.1f} min · '
                f'class clicks together every {args.burst_every:g} s · ' if args.burst_every else
                f'{args.url}{args.path} · {args.students} students · {wall_seconds / 60:.1f} min · ')
    settings += f'give up after {args.give_up:g} s' if args.give_up else 'never give up'
    fill = {
        'SETTINGS': html.escape(settings),
        'DATE': f'{datetime.now().astimezone():%Y-%m-%d %H:%M}',
        'TILES': ''.join(f'<div class="tile"><span class="muted">{k}</span><b>{v}</b></div>' for k, v in tiles),
        'TIMELINE': worker_timeline(records, x_max, bursts),
        'QUEUES': queue_chart(records, x_max, bursts),
        'LATENCY': latency_chart(records, x_max, bursts, args.give_up),
        'STATUSES': ''.join(f'<tr><td>{html.escape(s)}</td><td>{n}</td></tr>'
                            for s, n in Counter(str(r['status']) for r in records).most_common()),
        'ERRORS': '<tr><td colspan="4">None.</td></tr>' if not failures else ''.join(f'<tr><td>{html.escape(str(status))}</td><td>{html.escape(error)}</td><td>{len(times)}</td>'
                          f'<td>{min(times):.0f}–{max(times):.0f} s</td></tr>' for (status, error), times in failures.items()),
    }
    page = (Path(__file__).with_name('stress_report.html')).read_text(encoding='utf-8')
    for key, value in fill.items():
        page = page.replace('{{' + key + '}}', str(value))
    Path(path).write_text(page, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--key', required=True)
    parser.add_argument('--path', default='/v1/simulate')
    parser.add_argument('--students', type=int, default=30)
    parser.add_argument('--minutes', type=float, default=10)
    parser.add_argument('--think-min', type=float, default=5)
    parser.add_argument('--think-max', type=float, default=30)
    parser.add_argument('--burst-every', type=float, default=120, help='seconds between whole-class clicks (0: never)')
    parser.add_argument('--give-up', type=float, default=60, help='seconds a student waits before retrying (0: never)')
    parser.add_argument('--constructions', help='JSON file with EPSM construction names')
    parser.add_argument('--report', default='report.html')
    args = parser.parse_args()

    constructions = json.load(open(args.constructions)) if args.constructions else None
    if args.path != '/v1/daylight' and not constructions:
        parser.error('--constructions is required for energy requests')

    records, lock, teacher = [], threading.Lock(), Teacher()
    t0 = time.time()
    stop_at = t0 + args.minutes * 60
    threads = [threading.Thread(target=student,
                                args=(i, args, constructions, t0, stop_at, teacher, records, lock))
               for i in range(args.students)]
    for thread in threads:
        thread.start()
    while args.burst_every and time.time() + args.burst_every < stop_at:
        time.sleep(args.burst_every)
        teacher.ring(time.time() - t0)
    for thread in threads:
        thread.join()
    wall_seconds = time.time() - t0

    ok = sorted(r['latency'] for r in records if r['status'] == 200)
    print(f'\n{len(records)} requests, status counts: {dict(Counter(r["status"] for r in records))}')
    if ok:
        print(f'latency s  median {statistics.median(ok):.1f}  max {ok[-1]:.1f}')
    write_report(args.report, args, records, teacher.times, wall_seconds)
    print(f'report: {args.report}')


if __name__ == '__main__':
    main()
