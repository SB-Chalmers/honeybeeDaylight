"""Tests for downloading a study's generated HBJSON model."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from api.main import app
from api import routes


class _FakeStore:
    def __init__(self, record):
        self.record = record

    def get(self, study_id):
        if self.record and self.record.study_id == study_id:
            return self.record
        return None


def test_download_hbjson_returns_attachment(tmp_path, monkeypatch):
    study_id = 'std_123456789abc'
    study_folder = tmp_path / study_id
    study_folder.mkdir()
    model = {'type': 'Model', 'identifier': 'Daylight_Model'}
    (study_folder / 'model.hbjson').write_text(json.dumps(model), encoding='utf-8')

    monkeypatch.setattr(routes, 'STUDIES_ROOT', str(tmp_path))
    monkeypatch.setattr(
        routes,
        'store',
        _FakeStore(SimpleNamespace(study_id=study_id, status='complete')),
    )

    response = TestClient(app).get(f'/v1/studies/{study_id}/model.hbjson')

    assert response.status_code == 200
    assert response.json() == model
    assert response.headers['content-type'].startswith('application/json')
    assert response.headers['content-disposition'] == (
        f'attachment; filename="{study_id}.hbjson"'
    )
    assert response.headers['cache-control'] == 'private, no-store'


def test_download_hbjson_rejects_unknown_study(tmp_path, monkeypatch):
    monkeypatch.setattr(routes, 'STUDIES_ROOT', str(tmp_path))
    monkeypatch.setattr(routes, 'store', _FakeStore(None))

    response = TestClient(app).get('/v1/studies/std_missing/model.hbjson')

    assert response.status_code == 404


def test_download_hbjson_reports_model_not_ready(tmp_path, monkeypatch):
    study_id = 'std_123456789abc'
    monkeypatch.setattr(routes, 'STUDIES_ROOT', str(tmp_path))
    monkeypatch.setattr(
        routes,
        'store',
        _FakeStore(SimpleNamespace(study_id=study_id, status='running')),
    )

    response = TestClient(app).get(f'/v1/studies/{study_id}/model.hbjson')

    assert response.status_code == 409
    assert response.json()['detail']['error'] == 'model_not_ready'
