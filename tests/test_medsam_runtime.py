import numpy as np
import pytest
from app.services.feature_protocol import FeatureSettings
from app.services.medsam_runtime import intensity_bounds, prepare_slice, status


def test_medsam_artifacts_and_modalities():
    model = status()
    assert model['available'], model
    assert model['modalities'] == ['CT', 'MR']


def test_intensities_and_non_square_image():
    volume = np.arange(32 * 64, dtype=np.float32).reshape(32, 64)
    assert intensity_bounds(volume, 'CT', 40, 400) == (-160, 240)
    bounds = intensity_bounds(volume, 'MR')
    assert bounds[0] > 0 and bounds[1] > bounds[0]
    tensor, size, scale = prepare_slice(volume, bounds)
    assert tensor.shape == (1, 3, 256, 256)
    assert size == (128, 256) and scale == 4
    assert tensor.dtype == np.float32 and np.isfinite(tensor).all()
    assert tensor.min() >= 0 and tensor.max() <= 1
    assert not tensor[:, :, 128:].any()


def test_feature_settings_are_bounded_and_reproducible():
    first = FeatureSettings(families=['shape', 'firstorder']).protocol('MR')
    second = FeatureSettings(families=['firstorder', 'shape', 'shape']).protocol('MR')
    assert first == second
    assert first['setting']['normalize']
    assert not FeatureSettings().protocol('CT')['setting']['normalize']
    with pytest.raises(ValueError):
        FeatureSettings(resample_mm=0.01)
    with pytest.raises(ValueError):
        FeatureSettings(bin_width=float('nan'))
