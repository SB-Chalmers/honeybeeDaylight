"""
Tests for simulation.results
──────────────────────────────
Uses temporary fake .res / .da files — no recipe execution needed.
"""

import os

import numpy as np
import pytest

from simulation.results import parse_annual, parse_df


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_df_folder(tmp_path, values):
    folder = tmp_path / 'daylight_factor' / 'results'
    folder.mkdir(parents=True)
    np.savetxt(folder / 'floor_grid.res', values)
    return str(tmp_path)


def _make_annual_folder(tmp_path, values):
    folder = tmp_path / 'annual_daylight' / 'metrics' / 'da'
    folder.mkdir(parents=True)
    np.savetxt(folder / 'floor_grid.da', values)
    return str(tmp_path)


# ── parse_df ──────────────────────────────────────────────────────────────────

class TestParseDf:
    def test_returns_expected_keys(self, tmp_path):
        result = parse_df(_make_df_folder(tmp_path, [1.0, 2.0, 3.0]))
        assert 'summary' in result
        assert 'values' in result

    def test_summary_values_correct(self, tmp_path):
        result = parse_df(_make_df_folder(tmp_path, [1.0, 2.0, 3.0]))
        assert result['summary']['mean_df'] == pytest.approx(2.0, abs=1e-3)
        assert result['summary']['min_df']  == pytest.approx(1.0, abs=1e-3)
        assert result['summary']['max_df']  == pytest.approx(3.0, abs=1e-3)

    def test_values_list_length_matches_sensors(self, tmp_path):
        data = list(range(1, 11))
        result = parse_df(_make_df_folder(tmp_path, data))
        assert len(result['values']) == 10

    def test_values_are_plain_floats(self, tmp_path):
        result = parse_df(_make_df_folder(tmp_path, [1.5, 2.5]))
        assert all(isinstance(v, float) for v in result['values'])

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises((FileNotFoundError, OSError)):
            parse_df(str(tmp_path))


# ── parse_annual ──────────────────────────────────────────────────────────────

class TestParseAnnual:
    def test_returns_expected_keys(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [40.0, 60.0, 70.0]))
        assert 'summary' in result
        assert 'values' in result
        assert 'pass' in result

    def test_pass_mask_correct_at_50pct_threshold(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [40.0, 50.0, 60.0]),
                               sda_target_pct=50.0)
        assert result['pass'] == [False, True, True]

    def test_sda_summary_percentage(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [40.0, 60.0, 80.0]),
                               sda_target_pct=50.0)
        assert result['summary']['sda_300_50'] == pytest.approx(200 / 3, abs=0.1)
        assert result['summary']['sensor_count'] == 3
        assert result['summary']['passing_sensors'] == 2

    def test_values_and_pass_same_length(self, tmp_path):
        data = [float(i * 10) for i in range(20)]
        result = parse_annual(_make_annual_folder(tmp_path, data))
        assert len(result['values']) == len(result['pass']) == 20

    def test_all_fail_gives_zero_sda(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [10.0, 20.0, 30.0]),
                               sda_target_pct=50.0)
        assert result['summary']['sda_300_50'] == 0.0
        assert result['summary']['passing_sensors'] == 0

    def test_all_pass_gives_100_sda(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [60.0, 70.0, 80.0]),
                               sda_target_pct=50.0)
        assert result['summary']['sda_300_50'] == pytest.approx(100.0)

    def test_custom_threshold(self, tmp_path):
        result = parse_annual(_make_annual_folder(tmp_path, [40.0, 60.0, 80.0]),
                               sda_target_pct=70.0)
        assert result['pass'] == [False, False, True]
