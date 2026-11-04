"""Load test: N students sending unique requests at the same time for M minutes.

Each simulated student sends a request with a randomised building, waits for
the answer, thinks for a few seconds, and repeats. Prints latency and error
statistics at the end.

    python scripts/stress_test.py --url http://<host>:8000 --key <key> \
        --students 20 --minutes 20 --constructions constructions.json

``constructions.json`` holds {"wall": ..., "floor": ..., "roof": ..., "window": ...}
with real EPSM names (needed for /v1/energy and /v1/simulate).
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import threading
import time
from collections import Counter

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


def student(index, args, constructions, stop_at, records, lock):
    rng = random.Random(index)
    with httpx.Client(base_url=args.url, timeout=1800) as client:
        while time.time() < stop_at:
            started = time.perf_counter()
            try:
                status = post(client, args.path, random_building(rng, constructions),
                              args.key).status_code
            except httpx.HTTPError as exc:
                status = type(exc).__name__
            elapsed = time.perf_counter() - started
            with lock:
                records.append((status, elapsed))
                print(f'student {index:2d}: {status} in {elapsed:6.1f}s', flush=True)
            time.sleep(rng.uniform(args.think_min, args.think_max))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--key', required=True)
    parser.add_argument('--path', default='/v1/simulate')
    parser.add_argument('--students', type=int, default=20)
    parser.add_argument('--minutes', type=float, default=20)
    parser.add_argument('--think-min', type=float, default=5)
    parser.add_argument('--think-max', type=float, default=30)
    parser.add_argument('--constructions', help='JSON file with EPSM construction names')
    args = parser.parse_args()

    constructions = json.load(open(args.constructions)) if args.constructions else None
    if args.path != '/v1/daylight' and not constructions:
        parser.error('--constructions is required for energy requests')

    records, lock = [], threading.Lock()
    stop_at = time.time() + args.minutes * 60
    threads = [threading.Thread(target=student, args=(i, args, constructions, stop_at, records, lock))
               for i in range(args.students)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    ok = sorted(t for status, t in records if status == 200)
    print(f'\n{len(records)} requests, status counts: {dict(Counter(s for s, _ in records))}')
    if ok:
        p95 = ok[min(len(ok) - 1, int(len(ok) * 0.95))]
        print(f'latency s  median {statistics.median(ok):.1f}  p95 {p95:.1f}  max {ok[-1]:.1f}')
        print(f'throughput {len(ok) / args.minutes:.1f} successful requests/min')


if __name__ == '__main__':
    main()
