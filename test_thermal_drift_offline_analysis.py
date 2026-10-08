import unittest

import numpy as np

from standalone_thermal_drift import reanalyze_offline as analysis


class OfflineDecompositionTests(unittest.TestCase):
    def test_plane_center_is_measured_centroid(self):
        xx, yy = np.meshgrid(np.arange(0, 400, 20), np.arange(100, 500, 20))
        xy = np.column_stack((xx.ravel(), yy.ravel()))
        centered = (xy - xy.mean(axis=0)) / 1000
        delta = -.4 + centered @ np.array([.12, -.25])
        fit = analysis.fit_plane(xy, delta)
        self.assertAlmostEqual(fit['center_rad'], -.4)
        self.assertAlmostEqual(fit['x_rad_per_1000px'], .12)
        self.assertAlmostEqual(fit['y_rad_per_1000px'], -.25)

    def test_robust_fit_rejects_outliers(self):
        rng = np.random.default_rng(1)
        xy = rng.uniform(0, 1000, (1000, 2))
        delta = .2 + ((xy - xy.mean(axis=0)) / 1000) @ np.array([.1, -.2])
        delta[:25] += 1
        self.assertAlmostEqual(analysis.fit_plane(xy, delta)['center_rad'], .2, places=6)

    def test_circular_difference_crosses_wrap(self):
        difference = analysis.circular_difference(np.array([3.1, 3.0]), np.array([-3.0, -3.1]))
        np.testing.assert_allclose(difference, [2*np.pi-6.1, 2*np.pi-6.1])

    def test_local_linear_proxy_handles_uneven_sampling(self):
        time = np.array([0, 1, 3, 8, 9], dtype=float)
        np.testing.assert_allclose(analysis.innovation_proxy(time, 2*time+.3), 0, atol=1e-14)

    def test_proxy_matrix_shape(self):
        time = np.arange(5, dtype=float)
        values = np.column_stack((time, time*2))
        self.assertEqual(analysis.innovation_proxy(time, values).shape, (3, 2))
        np.testing.assert_allclose(analysis.innovation_proxy(time, values), 0)

    def test_exponential_fit_known_time_constant(self):
        time = np.r_[np.arange(11), np.arange(13, 71, 3)].astype(float)
        values = .01 - 1.5 * (1-np.exp(-np.maximum(time-1, 0)/13))
        result = analysis.exponential_fit(time, values, 70)
        self.assertAlmostEqual(result['tau_min'], 13)
        self.assertAlmostEqual(result['delay_min'], 1)
        self.assertLess(result['weighted_rms_rad'], 1e-10)


if __name__ == '__main__':
    unittest.main()
