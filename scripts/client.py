"""Reference client: signs a request, gzips it, sends it and unpacks the result.

    python scripts/client.py --url http://localhost:8000 --app heath --secret <secret>

Use it as a template for other clients (the signing is 5 lines).
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import hmac
import json
import time

import httpx
import numpy as np

DTYPES = {'uint16': '<u2', 'float32': '<f4'}

EXAMPLE = {
    'room': {
        'footprint_coordinates': [[0, 0], [8, 0], [8, 6], [0, 6]],
        'floors': 2,
        'wwr': 0.4,
    },
    'daylight': {'run_sda': True},
    'energy': {
        'constructions': {'wall': 'CHANGE-ME', 'floor': 'CHANGE-ME', 'roof': 'CHANGE-ME', 'window': 'CHANGE-ME'},
        'building_program': 'Office',
    },
}


def signed_post(client: httpx.Client, path: str, payload: dict, app: str, secret: str) -> httpx.Response:
    body = json.dumps(payload, separators=(',', ':')).encode()
    ts = str(int(time.time()))
    message = f'{ts}\nPOST\n{path}\n{hashlib.sha256(body).hexdigest()}'
    headers = {
        'content-type': 'application/json',
        'content-encoding': 'gzip',
        'x-app-id': app,
        'x-timestamp': ts,
        'x-signature': hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest(),
    }
    return client.post(path, content=gzip.compress(body), headers=headers)


def unpack(value):
    """Turn a packed array ({dtype, scale, shape, data}) back into numbers."""
    if not isinstance(value, dict) or 'data' not in value:
        return value
    raw = np.frombuffer(base64.b64decode(value['data']), dtype=DTYPES[value['dtype']])
    return (raw / value['scale']).reshape(value['shape'])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:8000')
    parser.add_argument('--app', default='heath')
    parser.add_argument('--secret', required=True)
    parser.add_argument('--path', default='/v1/simulate', choices=['/v1/simulate', '/v1/daylight', '/v1/energy'])
    parser.add_argument('--payload', help='JSON file with the request (default: built-in example)')
    args = parser.parse_args()

    payload = json.load(open(args.payload)) if args.payload else EXAMPLE
    with httpx.Client(base_url=args.url, timeout=1800) as client:
        response = signed_post(client, args.path, payload, args.app, args.secret)
    print(response.status_code, f'{len(response.content)} bytes')
    data = response.json()
    if response.status_code != 200:
        print(data)
        return
    daylight = data.get('daylight', data if 'df' in data else None)
    energy = data.get('energy', data if 'total_energy_kwh' in data else None)
    if daylight:
        df = unpack(daylight['df'])
        print('daylight', daylight['df_summary'], daylight['sda_summary'], f'{df.size} sensors')
    if energy:
        print('energy', {k: v for k, v in energy.items() if not isinstance(v, (dict, list))})


if __name__ == '__main__':
    main()
