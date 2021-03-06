import logging
from functools import partial

import numpy as np
import sparse
import matplotlib.cm as cm

from libertem import masks
from libertem.viz import CMAP_CIRCULAR_DEFAULT, visualize_simple, cmaps
from .base import AnalysisResult, AnalysisResultSet
from .masks import BaseMasksAnalysis


log = logging.getLogger(__name__)


class RadialFourierResultSet(AnalysisResultSet):
    """
    Result set of a :class:`RadialFourierAnalysis`

    Running a :class:`RadialFourierAnalysis` via :meth:`libertem.api.Context.run` on a dataset
    returns an instance of this class. It contains the Fourier coefficients
    for each bin. See :meth:`libertem.api.Context.create_radial_fourier_analysis` for
    available parameters and :ref:`radialfourier app` for a description of the application!

    .. versionadded:: 0.3.0

    Attributes
    ----------
    dominant_0, absolute_0_0, absolute_0_1, ..., absolute_0_<max_order>,\
    phase_0_0, ..., phase_0_<max_order>,\
    complex_0_0, ..., complex_0_<max_order>;\
    dominant_1, absolute_1_0, ..., complex_1_<max_order>;\
    dominant_<nbins-1>, ..., complex_<nbins-1>_<max_order> : libertem.analysis.base.AnalysisResult
        Results for each bin: dominant Fourier coefficient (indicates symmetry),
        absolute values of each Fourier coefficient,
        phase values of each Fourier coefficient, complex values of each Fourier coefficient.
        The results have the shape of the navigation dimension.
    raw_results : numpy.ndarray
        Complex numbers, shape (<n_bins>, <max_order + 1>, \\*(<ds.shape.nav>))
    """
    pass


class RadialFourierAnalysis(BaseMasksAnalysis):
    '''
    The Radial Fourier Analysis can be used to characterize
    atomic ordering in materials, in particular for low intensities where
    Fluctualtion EM :cite:`Gibson1997` has a hard time to distinguish speckle
    from shot noise. Reference :cite:`6980942` describes a previous application
    of this method to characterize features in images.

    This analysis doesn't use fast Fourier transforms, but calculates the
    Fourier coefficients using sparse matrices in a dot product following the
    `definition of Fourier series
    <https://en.wikipedia.org/wiki/Fourier_series#Complex-valued_functions>`_.

    See :meth:`libertem.api.Context.create_radial_fourier_analysis` for
    available parameters and :ref:`radialfourier app` for a description of the
    application!
    '''

    TYPE = 'UDF'

    def get_udf_results(self, udf_results, roi):
        # Here, we reconstruct the shape of the Job result
        # so that we don't have to change the involved
        # data processing in get_results
        # FIXME port this to the native layout as soon as
        # the Job interface is retired #550
        data = udf_results['intensity'].data
        job_results = data.reshape((np.prod(self.dataset.shape.nav), -1)).T
        return self.get_results(job_results)

    def get_results(self, job_results):
        '''
        The AnalysisResults are calculated lazily in this function to reduce
        overhead.
        '''
        shape = tuple(self.dataset.shape.nav)
        orders = self.parameters['max_order'] + 1
        n_bins = self.parameters['n_bins']
        job_results = job_results.reshape((n_bins, orders, *shape))

        def resultlist():
            sets = []
            absolute = np.absolute(job_results)
            normal = np.maximum(1, absolute[:, 0])
            min_absolute = np.min(absolute[:, 1:, ...] / normal[:, np.newaxis, ...])
            max_absolute = np.max(absolute[:, 1:, ...] / normal[:, np.newaxis, ...])
            angle = np.angle(job_results)
            threshold_map = absolute[:, 1:, ...].reshape((n_bins, -1)).max(axis=1) * 0.2
            below_threshold = np.all(
                absolute[:, 1:, ...] < threshold_map[:, np.newaxis, np.newaxis, np.newaxis],
                axis=1
            )
            dominant = np.argmax(absolute[:, 1:], axis=1) + 1
            dominant[below_threshold] = 0
            for b in range(n_bins):
                sets.append(
                    AnalysisResult(
                        raw_data=dominant[b],
                        visualized=partial(
                            visualize_simple, dominant[b], colormap=cm.tab20, vmin=0, vmax=20
                        ),
                        key="dominant_%s" % b,
                        title="dominant order of bin %s" % b,
                        desc="Dominant Fourier component",
                    )
                )
                sets.append(
                    AnalysisResult(
                        raw_data=absolute[b, 0],
                        visualized=partial(visualize_simple, absolute[b, 0]),
                        key="absolute_%s_%s" % (b, 0),
                        title="absolute of bin %s order %s" % (b, 0),
                        desc="Absolute value of Fourier component",
                    )
                )
                for o in range(1, orders):
                    sets.append(
                        AnalysisResult(
                            raw_data=absolute[b, o],
                            visualized=partial(visualize_simple,
                                absolute[b, o] / normal[b], vmin=min_absolute, vmax=max_absolute
                            ),
                            key="absolute_%s_%s" % (b, o),
                            title="absolute of bin %s order %s" % (b, o),
                            desc="Absolute value of Fourier component",
                        )
                    )
            for b in range(n_bins):
                for o in range(orders):
                    sets.append(
                        AnalysisResult(
                            raw_data=angle[b, o],
                            visualized=partial(visualize_simple,
                                angle[b, o], colormap=cmaps['perception_circular']
                            ),
                            key="phase_%s_%s" % (b, o),
                            title="phase of bin %s order %s" % (b, o),
                            desc="Phase of Fourier component",
                        )
                    )
            for b in range(n_bins):
                data = job_results[b, 0]
                f = partial(CMAP_CIRCULAR_DEFAULT.rgb_from_vector, (data.imag, data.real))
                sets.append(
                    AnalysisResult(
                        raw_data=data,
                        visualized=f,
                        key="complex_%s_%s" % (b, 0),
                        title="bin %s order %s" % (b, 0),
                        desc="Fourier component",
                    )
                )
                for o in range(1, orders):
                    data = job_results[b, o] / normal[b]
                    f = partial(CMAP_CIRCULAR_DEFAULT.rgb_from_vector,
                        (data.imag, data.real), vmax=max_absolute
                    )
                    sets.append(
                        AnalysisResult(
                            raw_data=data,
                            visualized=f,
                            key="complex_%s_%s" % (b, o),
                            title="bin %s order %s" % (b, o),
                            desc="Fourier component",
                        )
                    )
            return sets
        return RadialFourierResultSet(resultlist, raw_results=job_results)

    def get_mask_factories(self):
        if self.dataset.shape.sig.dims != 2:
            raise ValueError("can only handle 2D signals currently")

        (detector_y, detector_x) = self.dataset.shape.sig
        p = self.parameters

        cx = p['cx']
        cy = p['cy']
        ri = p['ri']
        ro = p['ro']
        n_bins = p['n_bins']
        max_order = p['max_order']

        use_sparse = p['use_sparse']

        def stack():
            rings = masks.radial_bins(
                centerX=cx,
                centerY=cy,
                imageSizeX=detector_x,
                imageSizeY=detector_y,
                radius=ro,
                radius_inner=ri,
                n_bins=n_bins,
                use_sparse=use_sparse,
                dtype=np.complex64
            )

            orders = np.arange(max_order + 1)

            r, phi = masks.polar_map(
                centerX=cx,
                centerY=cy,
                imageSizeX=detector_x,
                imageSizeY=detector_y
            )
            modulator = np.exp(phi * orders[:, np.newaxis, np.newaxis] * 1j)

            if use_sparse:
                rings = rings.reshape((rings.shape[0], 1, *rings.shape[1:]))
                ring_stack = [rings] * len(orders)
                ring_stack = sparse.concatenate(ring_stack, axis=1)
                ring_stack *= modulator
            else:
                ring_stack = rings[:, np.newaxis, ...] * modulator
            return ring_stack.reshape((-1, detector_y, detector_x))
        return stack

    def get_parameters(self, parameters):
        (detector_y, detector_x) = self.dataset.shape.sig

        cx = parameters.get('cx', detector_x / 2)
        cy = parameters.get('cy', detector_y / 2)
        ri = parameters.get('ri', 0)
        ro = parameters.get(
            'ro',
            masks.bounding_radius(cx, cy, detector_x, detector_y)
        )
        n_bins = parameters.get('n_bins', 1)
        max_order = parameters.get('max_order', 24)

        mask_count = n_bins * (max_order + 1)
        bin_width = (ro - ri) / n_bins
        bin_area = np.pi * ro**2 - np.pi * (ro - bin_width)**2
        stack_size = mask_count * detector_y * detector_x * 8

        default = 'scipy.sparse'
        # If the mask stack comfortably fits the L3 cache
        # FIXME more testing for optimum backend
        if stack_size < 2**18:
            default = False
        # Masks are actually dense
        elif bin_area / (detector_x * detector_y) > 0.05 and n_bins < 10:
            default = False

        use_sparse = parameters.get('use_sparse', default)
        return {
            'cx': cx,
            'cy': cy,
            'ri': ri,
            'ro': ro,
            'n_bins': n_bins,
            'max_order': max_order,
            'use_sparse': use_sparse,
            'mask_count': mask_count,
            'mask_dtype': np.complex64,
        }
