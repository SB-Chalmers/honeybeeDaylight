"""Tests for daylight worker result-contract validation."""

import pytest

from api.worker import _validate_daylight_result_alignment


def test_result_alignment_accepts_equal_ordered_arrays():
    _validate_daylight_result_alignment(
        [[0, 0, 0], [1, 0, 0]],
        {'values': [2.0, 3.0]},
        {'values': [40.0, 60.0], 'pass': [False, True]},
    )


def test_result_alignment_rejects_mismatched_arrays():
    with pytest.raises(RuntimeError, match='not index-aligned'):
        _validate_daylight_result_alignment(
            [[0, 0, 0], [1, 0, 0]],
            {'values': [2.0, 3.0]},
            {'values': [40.0], 'pass': [False]},
        )
