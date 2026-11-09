"""API: API keys, routing to the Celery tasks (run eagerly)."""

import pytest
from fastapi.testclient import TestClient

from api import auth
from api.main import app
from worker import celery_app, jobs

ROOM = {'footprint_coordinates': [[0, 0], [4, 0], [4, 4], [0, 4]]}
ENERGY = {'constructions': {'wall': 'w', 'floor': 'f', 'roof': 'r', 'window': 'g'}}
DAYLIGHT_RESULT = {
    'weather': 'default:x.epw', 'seconds': 0.1, 'sensor_count': 1, 'sensor_grids': [],
    'sensor_points': [[0, 0, 0.75]], 'df_summary': {'mean_df': 2, 'min_df': 2, 'max_df': 2},
    'df': [2.0], 'sda_summary': None, 'da': None,
}
ENERGY_RESULT = {
    'weather': 'default:x.epw', 'seconds': 0.1, 'floor_area_m2': 16, 'heating_energy_kwh': 1,
    'cooling_energy_kwh': 1, 'total_energy_kwh': 2, 'heating_demand_kwh_m2': 0.1,
    'cooling_demand_kwh_m2': 0.1, 'total_energy_kwh_m2': 0.1,
}


@pytest.fixture
def client(monkeypatch):
    celery_app.app.conf.task_always_eager = True
    monkeypatch.setattr(jobs, 'daylight_job', lambda request: DAYLIGHT_RESULT)
    monkeypatch.setattr(jobs, 'energy_job', lambda request: ENERGY_RESULT)
    monkeypatch.setattr(auth, 'APP_KEYS', {'secret': 'heath'})
    yield TestClient(app)
    celery_app.app.conf.task_always_eager = False


def post(client, path, payload, key='secret'):
    return client.post(path, json=payload, headers={'authorization': f'Bearer {key}'})


def test_simulate_returns_both_results(client):
    response = post(client, '/v1/simulate', {'room': ROOM, 'energy': ENERGY})
    assert response.status_code == 200, response.text
    assert response.json()['daylight']['df'] == [2.0]
    assert response.json()['energy']['floor_area_m2'] == 16


def test_daylight_and_energy_endpoints(client):
    assert post(client, '/v1/daylight', {'room': ROOM}).status_code == 200
    assert post(client, '/v1/energy', {'room': ROOM, 'energy': ENERGY}).status_code == 200


def test_wrong_key_is_rejected(client):
    response = post(client, '/v1/daylight', {'room': ROOM}, key='wrong')
    assert response.status_code == 401
    assert response.json()['error'] == 'unauthorized'


def test_request_without_key_is_rejected(client):
    assert client.post('/v1/daylight', json={'room': ROOM}).status_code == 401


def test_bad_input_from_worker_is_400(client, monkeypatch):
    def bad(request):
        raise jobs.BadInput('Unable to build the model')

    monkeypatch.setattr(jobs, 'daylight_job', bad)
    response = post(client, '/v1/daylight', {'room': ROOM})
    assert response.status_code == 400
    assert response.json() == {'error': 'bad_input', 'message': 'Unable to build the model'}


def test_validation_error_is_422(client):
    response = post(client, '/v1/daylight', {'room': {'footprint_coordinates': [[0, 0]]}})
    assert response.status_code == 422


def test_job_key_ignores_fields_the_task_does_not_read():
    from api.jobs import job_key

    base = {'room': ROOM, 'context_buildings': [], 'epw': None, 'pack': True, 'daylight': {}, 'energy': ENERGY}
    other_energy = {**base, 'energy': {**ENERGY, 'building_program': 'School'}}
    other_room = {**base, 'room': {'footprint_coordinates': [[0, 0], [5, 0], [5, 4], [0, 4]]}}
    assert job_key('simulation.daylight', base) == job_key('simulation.daylight', other_energy)
    assert job_key('simulation.energy', base) != job_key('simulation.energy', other_energy)
    assert job_key('simulation.daylight', base) != job_key('simulation.daylight', other_room)
    assert job_key('simulation.daylight', base) != job_key('simulation.energy', base)
