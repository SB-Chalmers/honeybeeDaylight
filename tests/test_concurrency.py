"""Worker concurrency chosen from the hardware."""

from worker import concurrency


def test_override_wins(monkeypatch):
    monkeypatch.setenv('ENERGY_CONCURRENCY', '3')
    monkeypatch.setenv('DAYLIGHT_CONCURRENCY', '5')
    assert concurrency.concurrency('energy') == 3
    assert concurrency.concurrency('daylight') == 5


def test_energy_uses_physical_cores(monkeypatch):
    monkeypatch.delenv('ENERGY_CONCURRENCY', raising=False)
    monkeypatch.setattr(concurrency, 'physical_cores', lambda: 8)
    assert concurrency.concurrency('energy') == 8


def test_daylight_uses_cores_with_a_gpu_or_one_without(monkeypatch):
    monkeypatch.delenv('DAYLIGHT_CONCURRENCY', raising=False)
    monkeypatch.setattr(concurrency, 'physical_cores', lambda: 8)
    monkeypatch.setattr(concurrency, 'gpu_count', lambda: 1)
    assert concurrency.concurrency('daylight') == 8
    monkeypatch.setattr(concurrency, 'gpu_count', lambda: 0)
    assert concurrency.concurrency('daylight') == 1


def test_physical_cores_is_positive():
    assert concurrency.physical_cores() >= 1
