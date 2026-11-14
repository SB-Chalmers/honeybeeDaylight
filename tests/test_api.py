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


@pytest.mark.parametrize('path', ['/v1/daylight', '/v1/energy', '/v1/energy-rc', '/v1/grasshopper'])
def test_impossible_geometry_is_422_on_every_endpoint(client, path):
    """Checked in the API, so no worker slot is spent on it."""
    room = {'footprint_coordinates': [[0, 0], [4, 0], [8, 0]]}
    response = post(client, path, {'room': room, 'energy': ENERGY})
    assert response.status_code == 422
    assert 'enclose 0.00' in response.json()['message']


def test_job_key_ignores_fields_the_task_does_not_read():
    from api.jobs import job_key

    base = {'room': ROOM, 'context_buildings': [], 'epw': None, 'pack': True, 'daylight': {}, 'energy': ENERGY}
    other_energy = {**base, 'energy': {**ENERGY, 'building_program': 'School'}}
    other_room = {**base, 'room': {'footprint_coordinates': [[0, 0], [5, 0], [5, 4], [0, 4]]}}
    assert job_key('simulation.daylight', base) == job_key('simulation.daylight', other_energy)
    assert job_key('simulation.energy', base) != job_key('simulation.energy', other_energy)
    assert job_key('simulation.daylight', base) != job_key('simulation.daylight', other_room)
    assert job_key('simulation.daylight', base) != job_key('simulation.energy', base)


def test_every_task_has_a_job_key():
    """A task missing from FIELDS only fails on the real stack (tests run Celery eagerly)."""
    from api.jobs import FIELDS, app

    assert set(FIELDS) == {name for name in app.tasks if name.startswith('simulation.')}


def test_grasshopper_file_holds_the_request(client):
    import json
    import xml.etree.ElementTree as ET
    from api.grasshopper import _objects

    room = {'footprint_coordinates': [[0, 0], [9, 0], [9, 7], [0, 7]], 'floors': 40,
            'wwr_by_cardinal': {'north': 0.1, 'east': 0.2, 'south': 0.6, 'west': 0.3}}
    response = post(client, '/v1/grasshopper', {'room': room, 'energy': ENERGY})
    assert response.status_code == 200, response.text
    assert 'heath.ghx' in response.headers['content-disposition']
    objects = dict(_objects(ET.fromstring(response.content)))

    def item(nick, name):
        return objects[nick].find(f".//item[@name='{name}']").text

    sent = json.loads(item('request', 'UserText'))
    assert sent['room']['footprint_coordinates'] == room['footprint_coordinates']
    assert 'pack' not in sent
    assert float(item('_floors_', 'Value')) == 40 <= float(item('_floors_', 'Max'))
    assert float(item('wwr_south_', 'Value')) == 0.6
    assert item('all_floors_', 'ToggleValue') == 'false'
    assert 'def sensor_points' in item('Heath Sensors', 'CodeInput')


def test_grasshopper_file_takes_a_single_wwr_like_heath_sends(client):
    import xml.etree.ElementTree as ET
    from api.grasshopper import _objects

    room = {'footprint_coordinates': [[0, 0], [9, 0], [9, 7], [0, 7]], 'wwr': 0.45}
    response = post(client, '/v1/grasshopper', {'room': room})
    assert response.status_code == 200, response.text
    objects = dict(_objects(ET.fromstring(response.content)))
    for nick in ('wwr_north_', 'wwr_east_', 'wwr_south_', 'wwr_west_'):
        assert float(objects[nick].find(".//item[@name='Value']").text) == 0.45
