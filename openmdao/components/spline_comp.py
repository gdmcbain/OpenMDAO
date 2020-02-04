"""Define the SplineComp class."""
from six import iteritems

import numpy as np

from openmdao.components.interp_util.interp import InterpND
from openmdao.core.explicitcomponent import ExplicitComponent

ALL_METHODS = ('cubic', 'slinear', 'lagrange2', 'lagrange3', 'akima',
               'bsplines')


class SplineComp(ExplicitComponent):
    """
    Interpolation component that can use any of OpenMDAO's interpolation methods.

    Attributes
    ----------
    interp_to_cp : dict
        Dictionary of relationship between the interpolated data and its control points.
    interps : dict
        Dictionary of interpolations for each output.
    _n_cp = int
        Number of control points.
    _spline_cache : list
        Cached arguements passed to add_spline. These are processed in setup.
    """

    def __init__(self, **kwargs):
        """
        Initialize all attributes.

        Parameters
        ----------
        **kwargs : dict
            Interpolator options to pass onward.
        """
        super(SplineComp, self).__init__(**kwargs)

        self.interp_to_cp = {}
        self.interps = {}
        self._spline_cache = []
        self._n_cp = None

    def _declare_options(self):
        """
        Declare options.
        """
        super(SplineComp, self)._declare_options()

        self.options.declare('vec_size', types=int, default=1,
                             desc='Number of points to evaluate at once.')
        self.options.declare('method', values=ALL_METHODS, default='akima',
                             desc='Spline interpolation method to use for all outputs.')
        self.options.declare('x_interp_val', types=(list, np.ndarray),
                             desc='List/array of x interpolated point values.')
        self.options.declare('x_cp_val', default=None, types=(list, np.ndarray), allow_none=True,
                             desc='List/array of x control point values, must be monotonically '
                             'increasing. Not applicable for bsplines.')
        self.options.declare('num_cp', default=None, types=(int, ), allow_none=True,
                             desc='Number of spline control points. Optional alternative to '
                             'x_cp_val. Required for bsplines.')
        self.options.declare('interp_options', types=dict, default={},
                             desc='Dict contains the name and value of options specific to the '
                             'chosen interpolation method.')

    def add_spline(self, y_cp_name, y_interp_name, y_cp_val=None, y_units=None):
        """
        Add a single spline output to this component.

        Parameters
        ----------
        y_cp_name : str
            Name for the y control points input.
        y_interp_name : str
            Name of the y interpolated points output.
        y_cp_val : list or ndarray
            List/array of default y control point values.
        y_units : str or None
            Units of the y variable.
        """
        self._spline_cache.append((y_cp_name, y_interp_name, y_cp_val, y_units))

    def setup(self):
        """
        Perform some final setup and checks.
        """
        interp_method = self.options['method']

        x_cp_val = self.options['x_cp_val']
        n_cp = self.options['num_cp']

        if x_cp_val is not None:

            if interp_method == 'bsplines':
                msg = "{}: 'x_cp_val' is not a valid option when using method 'bsplines'. "
                msg += "Set 'num_cp' instead."
                raise ValueError(msg.format(self.msginfo))

            if n_cp is not None:
                msg = "{}: It is not valid to set both options 'x_cp_val' and 'num_cp'."
                raise ValueError(msg.format(self.msginfo))

            grid = np.asarray(x_cp_val)
            n_cp = len(grid)

        elif n_cp is not None:
            grid = np.arange(n_cp)

        else:
            msg = "{}: Either option 'x_cp_val' or 'num_cp' must be set."
            raise ValueError(msg.format(self.msginfo))

        self._n_cp = n_cp
        opts = {}
        if 'interp_options' in self.options:
            opts = self.options['interp_options']

        vec_size = self.options['vec_size']
        n_interp = len(self.options['x_interp_val'])

        for y_cp_name, y_interp_name, y_cp_val, y_units in self._spline_cache:

            self.add_output(y_interp_name, np.ones((vec_size, n_interp)), units=y_units)

            if y_cp_val is None:
                y_cp_val = np.ones((vec_size, n_cp))

            elif len(y_cp_val.shape) < 2:
                y_cp_val = y_cp_val.reshape((vec_size, n_cp))

            self.add_input(name=y_cp_name, val=y_cp_val)

            self.interp_to_cp[y_interp_name] = y_cp_name

            row = np.repeat(np.arange(n_interp), n_cp)
            col = np.tile(np.arange(n_cp), n_interp)
            rows = np.tile(row, vec_size) + np.repeat(n_interp * np.arange(vec_size), n_interp * n_cp)
            cols = np.tile(col, vec_size) + np.repeat(n_cp * np.arange(vec_size), n_interp * n_cp)

            self.declare_partials(y_interp_name, y_cp_name, rows=rows, cols=cols)

            # Separate data for each vec_size, but we only need to do sizing, so just pass
            # in the first.  Most interps aren't vectorized.
            cp_val = y_cp_val[0, :]
            self.interps[y_interp_name] = InterpND((grid, ), cp_val,
                                                   interp_method=interp_method,
                                                   x_interp=self.options['x_interp_val'],
                                                   bounds_error=False, **opts)

        # The scipy methods do not support complex step.
        if self.options['method'].startswith('scipy'):
            self.set_check_partial_options('*', method='fd')

    def compute(self, inputs, outputs):
        """
        Perform the interpolation at run time.

        Parameters
        ----------
        inputs : Vector
            unscaled, dimensional input variables read via inputs[key]
        outputs : Vector
            unscaled, dimensional output variables read via outputs[key]
        """
        for out_name, interp in iteritems(self.interps):
            values = inputs[self.interp_to_cp[out_name]]
            interp._compute_d_dvalues = True
            interp._compute_d_dx = False
            interp.x_interp = self.options['x_interp_val']

            try:
                outputs[out_name] = interp.evaluate_spline(values)

            except ValueError as err:
                msg = "{}: Error interpolating output '{}':\n{}"
                raise ValueError(msg.format(self.msginfo, out_name, str(err)))

    def compute_partials(self, inputs, partials):
        """
        Collect computed partial derivatives and return them.

        Checks if the needed derivatives are cached already based on the
        inputs vector. Refreshes the cache by re-computing the current point
        if necessary.

        Parameters
        ----------
        inputs : Vector
            unscaled, dimensional input variables read via inputs[key]
        partials : Jacobian
            sub-jac components written to partials[output_name, input_name]
        """
        vec_size = self.options['vec_size']
        n_interp = len(self.options['x_interp_val'])
        n_cp = self._n_cp

        for out_name, interp in iteritems(self.interps):
            cp_name = self.interp_to_cp[out_name]

            d_dvalues = interp._d_dvalues
            if d_dvalues is not None:
                dy_ddata = np.zeros((vec_size, n_interp, n_cp))

                if d_dvalues.shape[0] == vec_size:
                    # Akima precomputes derivs at all points in vec_size.
                    dy_ddata[:] = d_dvalues
                else:
                    # Bsplines computed derivative is the same at all points in vec_size.
                    dy_ddata[:] = np.broadcast_to(d_dvalues.toarray(), (vec_size, n_interp, n_cp))
            else:
                x_interp = self.options['x_interp_val']
                dy_ddata = np.zeros((n_interp, n_cp))

                # This way works for the rest of the interpolation methods.
                for k in range(n_interp):
                    val = interp.training_gradients(x_interp[k:k + 1])
                    dy_ddata[k, :] = val
                dy_ddata = np.broadcast_to(dy_ddata, (vec_size, n_interp, n_cp))

            partials[out_name, cp_name] = dy_ddata.flatten()
