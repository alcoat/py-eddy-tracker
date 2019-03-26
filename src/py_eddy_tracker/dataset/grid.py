# -*- coding: utf-8 -*-
"""
"""
import logging
from numpy import concatenate, int32, empty, maximum, where, array, \
    sin, deg2rad, pi, ones, cos, ma, int8, histogram2d, arange, float_, \
    linspace, errstate, int_, interp, meshgrid, nan, ceil, sinc, isnan, \
    floor, percentile, zeros, arctan2, arcsin
from numpy.linalg import lstsq
from datetime import datetime
from scipy.special import j1
from netCDF4 import Dataset
from scipy.ndimage import gaussian_filter, convolve
from scipy.interpolate import RectBivariateSpline, interp1d
from scipy.spatial import cKDTree
from scipy.signal import welch
from cv2 import filter2D
from numba import njit, prange, types as numba_types
from matplotlib.path import Path as BasePath
from matplotlib.contour import QuadContourSet as BaseQuadContourSet
from pyproj import Proj
from pint import UnitRegistry
from ..observations import EddiesObservations
from ..eddy_feature import Amplitude, Contours
from .. import VAR_DESCR


def raw_resample(datas, fixed_size):
    nb_value = datas.shape[0]
    if nb_value == 1:
        raise Exception()
    return interp(arange(fixed_size), arange(nb_value) * (fixed_size - 1) / (nb_value - 1) , datas)


@property
def mean_coordinates(self):
    return self.vertices.mean(axis=0)


BasePath.mean_coordinates = mean_coordinates


@property
def lon(self):
    return self.vertices[:, 0]


BasePath.lon = lon


@property
def lat(self):
    return self.vertices[:, 1]


BasePath.lat = lat


@njit(cache=True, fastmath=True)
def distance(lon0, lat0, lon1, lat1):
    D2R = pi / 180.
    sin_dlat = sin((lat1 - lat0) * 0.5 * D2R)
    sin_dlon = sin((lon1 - lon0) * 0.5 * D2R)
    cos_lat1 = cos(lat0 * D2R)
    cos_lat2 = cos(lat1 * D2R)
    a_val = sin_dlon ** 2 * cos_lat1 * cos_lat2 + sin_dlat ** 2
    return 6370997.0 * 2 * arctan2(a_val ** 0.5, (1 - a_val) ** 0.5)

@njit(cache=True)
def distance_vincenty(lon0, lat0, lon1, lat1):
    """ better than haversine but buggy ??"""
    D2R = pi / 180.
    dlon = (lon1 - lon0) * D2R
    cos_dlon = cos(dlon)
    cos_lat1 = cos(lat0 * D2R)
    cos_lat2 = cos(lat1 * D2R)
    sin_lat1 = sin(lat0 * D2R)
    sin_lat2 = sin(lat1 * D2R)
    return 6370997.0 * arctan2(
        ((cos_lat2 * sin(dlon) ** 2) + (cos_lat1 * sin_lat2 - sin_lat1 * cos_lat2 * cos_dlon) ** 2) ** .5,
        sin_lat1 * sin_lat2 + cos_lat1 * cos_lat2 * cos_dlon)


@njit(cache=True, fastmath=True)
def uniform_resample(x_val, y_val, num_fac=2, fixed_size=None):
    """
    Resample contours to have (nearly) equal spacing
       x_val, y_val    : input contour coordinates
       num_fac : factor to increase lengths of output coordinates
    """
    # Get distances
    dist = empty(x_val.shape)
    dist[0] = 0
    dist[1:] = distance(x_val[:-1], y_val[:-1], x_val[1:], y_val[1:])
    # To be still monotonous (dist is store in m)
    dist[1:][dist[1:]<1e-3] = 1e-3
    dist = dist.cumsum()
    # Get uniform distances
    if fixed_size is None:
        fixed_size = dist.size * num_fac
    d_uniform = linspace(0, dist[-1], fixed_size)
    x_new = interp(d_uniform, dist, x_val)
    y_new = interp(d_uniform, dist, y_val)
    return x_new, y_new


@njit(cache=True)
def uniform_resample_stack(vertices, num_fac=2, fixed_size=None):
    x_val, y_val = vertices[:, 0], vertices[:, 1]
    x_new, y_new = uniform_resample(x_val, y_val, num_fac, fixed_size)
    data = empty((x_new.shape[0], 2))
    data[:, 0] = x_new
    data[:, 1] = y_new
    return data


@njit(cache=True)
def value_on_regular_contour(x_g, y_g, z_g, m_g, vertices, num_fac=2, fixed_size=None):
    x_val, y_val = vertices[:, 0], vertices[:, 1]
    x_new, y_new = uniform_resample(x_val, y_val, num_fac, fixed_size)
    return interp2d_geo(x_g, y_g, z_g, m_g, x_new[1:], y_new[1:])


@njit(cache=True)
def mean_on_regular_contour(x_g, y_g, z_g, m_g, vertices, num_fac=2, fixed_size=None):
    x_val, y_val = vertices[:, 0], vertices[:, 1]
    x_new, y_new = uniform_resample(x_val, y_val, num_fac, fixed_size)
    return interp2d_geo(x_g, y_g, z_g, m_g, x_new[1:], y_new[1:]).mean()


def fit_circle_path(self):
    if not hasattr(self, '_circle_params'):
        self._circle_params = _fit_circle_path(self.vertices)
    return self._circle_params


@njit(cache=True, fastmath=True)
def _fit_circle_path(vertice):
    lons, lats = vertice[:, 0], vertice[:, 1]
    lon0, lat0 = lons.mean(), lats.mean()
    c_x, c_y = coordinates_to_local(lons, lats, lon0, lat0)
    # Some time, edge is only a dot of few coordinates
    d_lon = lons.max() - lons.min()
    d_lat = lats.max() - lats.min()
    if d_lon < 1e-7 and d_lat < 1e-7:
        # logging.warning('An edge is only define in one position')
        # logging.debug('%d coordinates %s,%s', len(lons),lons,
                      # lats)
        return 0, -90, nan, nan
    centlon_e, centlat_e, eddy_radius_e, aerr = fit_circle_c_numba(c_x, c_y)
    centlon_e, centlat_e = local_to_coordinates(centlon_e, centlat_e, lon0, lat0)
    centlon_e = (centlon_e - lon0 + 180) % 360 + lon0 - 180
    return centlon_e, centlat_e, eddy_radius_e, aerr


@njit(cache=True, fastmath=True)
def coordinates_to_local(lon, lat, lon0, lat0):
    D2R = pi / 180.
    R = 6370997
    dlon = (lon - lon0) * D2R
    sin_dlat = sin((lat - lat0) * 0.5 * D2R)
    sin_dlon = sin(dlon * 0.5)
    cos_lat0 = cos(lat0 * D2R)
    cos_lat = cos(lat * D2R)
    a_val = sin_dlon ** 2 * cos_lat0 * cos_lat + sin_dlat ** 2
    module = R * 2 * arctan2(a_val ** 0.5, (1 - a_val) ** 0.5)

    dx = lon - lon0
    dy = lat - lat0
    azimuth = pi /2 - arctan2(
        cos_lat * sin(dlon),
        cos_lat0 * sin(lat * D2R) - sin(lat0 * D2R) * cos_lat * cos(dlon))
    return module * cos(azimuth), module * sin(azimuth)


@njit(cache=True, fastmath=True)
def local_to_coordinates(x, y, lon0, lat0):
    D2R = pi / 180.
    R = 6370997
    d = (x ** 2 + y ** 2) ** .5 / R
    a = -(arctan2(y, x) -pi / 2)
    lat = arcsin(sin(lat0 * D2R) * cos(d) + cos(lat0 * D2R) * sin(d) * cos(a))
    lon = lon0 + arctan2(sin(a) * sin(d) * cos(lat0 * D2R), cos(d) - sin(lat0 * D2R) * sin(lat)) / D2R
    return lon, lat / D2R


@njit(cache=True)
def fit_circle_c_numba(x_vec, y_vec):
    nb_elt = x_vec.shape[0]
    p_inon_x = empty(nb_elt)
    p_inon_y = empty(nb_elt)

    x_mean = x_vec.mean()
    y_mean = y_vec.mean()

    norme = (x_vec - x_mean) ** 2 + (y_vec - y_mean) ** 2
    norme_max = norme.max()
    scale = norme_max ** .5

    # Form matrix equation and solve it
    # Maybe put f4
    datas = ones((nb_elt, 3))
    datas[:, 0] = 2. * (x_vec - x_mean) / scale
    datas[:, 1] = 2. * (y_vec - y_mean) / scale

    (center_x, center_y, radius), residuals, rank, s = lstsq(datas, norme / norme_max)

    # Unscale data and get circle variables
    radius += center_x ** 2 + center_y ** 2
    radius **= .5
    center_x *= scale
    center_y *= scale
    # radius of fitted circle
    radius *= scale
    # center X-position of fitted circle
    center_x += x_mean
    # center Y-position of fitted circle
    center_y += y_mean

    # area of fitted circle
    c_area = (radius ** 2) * pi

    # Find distance between circle center and contour points_inside_poly
    for i_elt in range(nb_elt):
        # Find distance between circle center and contour points_inside_poly
        dist_poly = ((x_vec[i_elt] - center_x) ** 2 + (y_vec[i_elt] - center_y) ** 2) ** .5
        # Indices of polygon points outside circle
        # p_inon_? : polygon x or y points inside & on the circle
        if dist_poly > radius:
            p_inon_y[i_elt] = center_y + radius * (y_vec[i_elt] - center_y) / dist_poly
            p_inon_x[i_elt] = center_x - (center_x - x_vec[i_elt]) * (center_y - p_inon_y[i_elt]) / (
                        center_y - y_vec[i_elt])
        else:
            p_inon_x[i_elt] = x_vec[i_elt]
            p_inon_y[i_elt] = y_vec[i_elt]

    # Area of closed contour/polygon enclosed by the circle
    p_area_incirc = 0
    p_area = 0
    for i_elt in range(nb_elt - 1):
        # Indices of polygon points outside circle
        # p_inon_? : polygon x or y points inside & on the circle
        p_area_incirc += p_inon_x[i_elt] * p_inon_y[1 + i_elt] - p_inon_x[i_elt + 1] * p_inon_y[i_elt]
        # Shape test
        # Area and centroid of closed contour/polygon
        p_area += x_vec[i_elt] * y_vec[1 + i_elt] - x_vec[1 + i_elt] * y_vec[i_elt]
    p_area = abs(p_area) * .5

    p_area_incirc = abs(p_area_incirc) * .5

    a_err = (c_area - 2 * p_area_incirc + p_area) * 100. / c_area
    return center_x, center_y, radius, a_err


BasePath.fit_circle = fit_circle_path


def pixels_in(self, grid):
    if not hasattr(self, '_slice'):
        self._slice = grid.bbox_indice(self.vertices)
    if not hasattr(self, '_pixels_in'):
        self._pixels_in = grid.get_pixels_in(self)
    return self._pixels_in


@property
def bbox_slice(self):
    if not hasattr(self, '_slice'):
        raise Exception('No pixels_in call before!')
    return self._slice


@property
def pixels_index(self):
    if not hasattr(self, '_slice'):
        raise Exception('No pixels_in call before!')
    return self._pixels_in


@property
def nb_pixel(self):
    if not hasattr(self, '_pixels_in'):
        raise Exception('No pixels_in call before!')
    return self._pixels_in[0].shape[0]


@njit(cache=True)
def is_left(x_line_0, y_line_0, x_line_1, y_line_1, x_test, y_test):
    """
    http://geomalgorithms.com/a03-_inclusion.html
    isLeft(): tests if a point is Left|On|Right of an infinite line.
    Input:  three points P0, P1, and P2
    Return: >0 for P2 left of the line through P0 and P1
            =0 for P2  on the line
            <0 for P2  right of the line
    See: Algorithm 1 "Area of Triangles and Polygons"
    """
    # Vector product
    product = (x_line_1 - x_line_0) * (y_test - y_line_0) - (x_test - x_line_0) * (y_line_1 - y_line_0)
    return product > 0


@njit(cache=True)
def poly_contain_poly(xy_poly_out, xy_poly_in):
    nb_elt = xy_poly_in.shape[0]
    for i_elt in prange(nb_elt):
        wn = winding_number_poly(xy_poly_in[i_elt, 0], xy_poly_in[i_elt, 1], xy_poly_out)
        if wn == 0:
            return False
    return True


@njit(cache=True)
def winding_number_poly(x, y, xy_poly):
    nb_elt = xy_poly.shape[0]
    wn = 0
    # loop through all edges of the polygon
    for i_elt in range(nb_elt):
        if i_elt + 1 == nb_elt:
            x_next = xy_poly[0, 0]
            y_next = xy_poly[0, 1]
        else:
            x_next = xy_poly[i_elt + 1, 0]
            y_next = xy_poly[i_elt + 1, 1]
        if xy_poly[i_elt, 1] <= y:
            if y_next > y:
                if is_left(xy_poly[i_elt, 0],
                           xy_poly[i_elt, 1],
                           x_next,
                           y_next,
                           x, y
                           ):
                    wn += 1
        else:
            if y_next <= y:
                if not is_left(xy_poly[i_elt, 0],
                               xy_poly[i_elt, 1],
                               x_next,
                               y_next,
                               x, y
                               ):
                    wn -= 1
    return wn


@njit(cache=True)
def winding_number_grid_in_poly(x_1d, y_1d, i_x0, i_x1, x_size, i_y0, xy_poly):
    """
    http://geomalgorithms.com/a03-_inclusion.html
    wn_PnPoly(): winding number test for a point in a polygon
          Input:   P = a point,
                   V[] = vertex points of a polygon V[n+1] with V[n]=V[0]
          Return:  wn = the winding number (=0 only when P is outside)
    """
    # the  winding number counter
    nb_x, nb_y = len(x_1d), len(y_1d)
    wn = empty((nb_x, nb_y), dtype=numba_types.bool_)
    for i in range(nb_x):
        x_pt = x_1d[i]
        for j in range(nb_y):
            y_pt = y_1d[j]
            wn[i, j] = winding_number_poly(x_pt, y_pt, xy_poly)
    i_x, i_y = where(wn)
    i_x += i_x0
    i_y += i_y0
    if i_x1 < i_x0:
        i_x %= x_size
    return i_x, i_y



BasePath.pixels_in = pixels_in
BasePath.pixels_index = pixels_index
BasePath.bbox_slice = bbox_slice
BasePath.nb_pixel = nb_pixel


class GridDataset(object):
    """
    Class to have basic tool on NetCDF Grid
    """

    __slots__ = (
        '_x_var',
        '_y_var',
        'x_c',
        'y_c',
        'x_bounds',
        'y_bounds',
        'centered',
        'xinterp',
        'yinterp',
        'x_dim',
        'y_dim',
        'coordinates',
        'filename',
        'dimensions',
        'variables_description',
        'global_attrs',
        'vars',
        'interpolators',
        'speed_coef',
        'contours',
    )

    GRAVITY = 9.807
    EARTH_RADIUS = 6378136.3
    N = 1

    def __init__(self, filename, x_name, y_name, centered=None):
        self.dimensions = None
        self.variables_description = None
        self.global_attrs = None
        self.x_c = None
        self.y_c = None
        self.x_bounds = None
        self.y_bounds = None
        self.x_dim = None
        self.y_dim = None
        self.centered = centered
        self.contours = None
        self.xinterp = None
        self.yinterp = None
        self.filename = filename
        self.coordinates = x_name, y_name
        self.vars = dict()
        self.interpolators = dict()
        logging.warning('We assume the position of grid is the center'
                        ' corner for %s', filename)
        self.load_general_features()
        self.load()

    @property
    def is_centered(self):
        """Give information if pixel is describe with center position or
        a corner
        """
        if self.centered is None:
            return True
        else:
            return self.centered

    def load_general_features(self):
        """Load attrs
        """
        logging.debug('Load general feature from %(filename)s', dict(filename=self.filename))
        with Dataset(self.filename) as h:
            # Load generals
            self.dimensions = {i: len(v) for i, v in h.dimensions.items()}
            self.variables_description = dict()
            for i, v in h.variables.items():
                args = (i, v.datatype)
                kwargs = dict(
                    dimensions=v.dimensions,
                    zlib=True,
                )
                if hasattr(v, '_FillValue'):
                    kwargs['fill_value'] = v._FillValue,
                attrs = dict()
                for attr in v.ncattrs():
                    if attr in kwargs.keys():
                        continue
                    if attr == '_FillValue':
                        continue
                    attrs[attr] = getattr(v, attr)
                self.variables_description[i] = dict(
                    args=args,
                    kwargs=kwargs,
                    attrs=attrs,
                    infos=dict())
            self.global_attrs = {attr: getattr(h, attr) for attr in h.ncattrs()}

    def write(self, filename):
        """Write dataset output with same format like input
        """
        with Dataset(filename, 'w') as h_out:
            for dimension, size in self.dimensions.items():
                test = False
                for varname, variable in self.variables_description.items():
                    if varname not in self.coordinates and varname not in self.vars.keys():
                        continue
                    if dimension in variable['kwargs']['dimensions']:
                        test = True
                        break
                if test:
                    h_out.createDimension(dimension, size)

            for varname, variable in self.variables_description.items():
                if varname not in self.coordinates and varname not in self.vars.keys():
                    continue
                var = h_out.createVariable(*variable['args'], **variable['kwargs'])
                for key, value in variable['attrs'].items():
                    setattr(var, key, value)

                infos = self.variables_description[varname]['infos']
                if infos.get('transpose', False):
                    var[:] = self.vars[varname].T
                else:
                    var[:] = self.vars[varname]

            for attr, value in self.global_attrs.items():
                setattr(h_out, attr, value)

    def load(self):
        """Load variable (data)
        """
        x_name, y_name = self.coordinates
        with Dataset(self.filename) as h:
            self.x_dim = h.variables[x_name].dimensions
            self.y_dim = h.variables[y_name].dimensions

            self.vars[x_name] = h.variables[x_name][:]
            self.vars[y_name] = h.variables[y_name][:]

            if self.is_centered:
                logging.info('Grid center')
                self.x_c = self.vars[x_name]
                self.y_c = self.vars[y_name]

                self.x_bounds = concatenate((
                    self.x_c, (2 * self.x_c[-1] - self.x_c[-2],)))
                self.y_bounds = concatenate((
                    self.y_c, (2 * self.y_c[-1] - self.y_c[-2],)))
                d_x = self.x_bounds[1:] - self.x_bounds[:-1]
                d_y = self.y_bounds[1:] - self.y_bounds[:-1]
                self.x_bounds[:-1] -= d_x / 2
                self.x_bounds[-1] -= d_x[-1] / 2
                self.y_bounds[:-1] -= d_y / 2
                self.y_bounds[-1] -= d_y[-1] / 2

            else:
                self.x_bounds = self.vars[x_name]
                self.y_bounds = self.vars[y_name]

                if len(self.x_dim) == 1:
                    raise Exception('not test')
                    self.x_c = (self.x_bounds[1:] + self.x_bounds[:-1]) / 2
                    self.y_c = (self.y_bounds[1:] + self.y_bounds[:-1]) / 2
                else:
                    raise Exception('not write')

        self.init_pos_interpolator()

    def is_circular(self):
        """Check grid circularity
        """
        return False

    def units(self, varname):
        with Dataset(self.filename) as h:
            var = h.variables[varname]
            if hasattr(var, 'units'):
                return var.units

    def grid(self, varname):
        """give grid required
        """
        if varname not in self.vars:
            coordinates_dims = list(self.x_dim)
            coordinates_dims.extend(list(self.y_dim))
            logging.debug('Load %(varname)s from %(filename)s', dict(varname=varname, filename=self.filename))
            with Dataset(self.filename) as h:
                dims = h.variables[varname].dimensions
                sl = [slice(None) if dim in coordinates_dims else 0 for dim in dims]
                self.vars[varname] = h.variables[varname][sl]
                if len(self.x_dim) == 1:
                    i_x = where(array(dims) == self.x_dim)[0][0]
                    i_y = where(array(dims) == self.y_dim)[0][0]
                    if i_x > i_y:
                        self.variables_description[varname]['infos']['transpose'] = True
                        self.vars[varname] = self.vars[varname].T
            if not hasattr(self.vars[varname], 'mask'):
                self.vars[varname] = ma.array(self.vars[varname], mask=zeros(self.vars[varname].shape, dtype='bool'))
        return self.vars[varname]

    def high_filter(self, grid_name, x_cut, y_cut):
        """create a high filter with a low one
        """
        result = self._low_filter(grid_name, x_cut, y_cut)
        self.vars[grid_name] -= result

    def low_filter(self, grid_name, x_cut, y_cut):
        """low filtering
        """
        result = self._low_filter(grid_name, x_cut, y_cut)
        self.vars[grid_name] -= self.vars[grid_name] - result

    @property
    def bounds(self):
        """Give bound
        """
        return self.x_bounds.min(), self.x_bounds.max(), self.y_bounds.min(), self.y_bounds.max()

    def eddy_identification(self, grid_height, uname, vname, date, step=0.005, shape_error=55,
                            array_sampling=50, pixel_limit=None, bbox_surface_min_degree=.125**2):
        if not isinstance(date, datetime):
            raise Exception('Date argument be a datetime object')
        # The inf limit must be in pixel and  sup limit in surface
        if pixel_limit is None:
            pixel_limit = (4, 1000)

        # Compute an interpolator for eke
        self.init_speed_coef(uname, vname)

        # Get h grid
        data = self.grid(grid_height)

        # Compute levels for ssh
        z_min, z_max = data.min(), data.max()
        d_z = z_max -z_min
        data_tmp = data[~data.mask]
        epsilon = 0.001  # in %
        z_min_p, z_max_p = percentile(data_tmp, epsilon), percentile(data_tmp, 100 - epsilon)
        d_zp = z_max_p - z_min_p
        if d_z / d_zp > 2:
            logging.warning('Maybe some extrema are present zmin %f (m) and zmax %f (m) will be replace by %f and %f',
                            z_min, z_max, z_min_p, z_max_p)
            z_min, z_max = z_min_p, z_max_p

        levels = arange(z_min - z_min % step, z_max - z_max % step + 2 * step, step)

        # Get x and y values
        x, y = self.x_c, self.y_c

        # Compute ssh contour
        self.contours = Contours(x, y, data, levels, bbox_surface_min_degree=bbox_surface_min_degree, wrap_x=self.is_circular())

        track_extra_variables = ['height_max_speed_contour', 'height_external_contour', 'height_inner_contour']
        array_variables = ['contour_lon_e', 'contour_lat_e', 'contour_lon_s', 'contour_lat_s', 'uavg_profile']
        # Compute cyclonic and anticylonic research:
        a_and_c = list()
        for anticyclonic_search in [True, False]:
            eddies = list()
            iterator = 1 if anticyclonic_search else -1

            # Loop over each collection
            for coll_ind, coll in enumerate(self.contours.iter(step=iterator)):
                corrected_coll_index = coll_ind
                if iterator == -1:
                    corrected_coll_index = - coll_ind - 1

                contour_paths = coll.get_paths()
                nb_paths = len(contour_paths)
                if nb_paths == 0:
                    continue
                cvalues = self.contours.cvalues[corrected_coll_index]
                logging.debug('doing collection %s, contour value %.4f, %d paths',
                              corrected_coll_index, cvalues, nb_paths)

                # Loop over individual c_s contours (i.e., every eddy in field)
                for current_contour in contour_paths:
                    if current_contour.used:
                        continue
                    centlon_e, centlat_e, eddy_radius_e, aerr = current_contour.fit_circle()

                    # Filter for shape
                    if aerr < 0 or aerr > shape_error:
                        continue
                    # Get indices of centroid
                    # Give only 1D array of lon and lat not 2D data
                    i_x, i_y = self.nearest_grd_indice(centlon_e, centlat_e)

                    # Check if centroid is on define value
                    if data.mask[i_x, i_y]:
                        continue
                    # Test to know cyclone or anticyclone
                    acyc_not_cyc = data[i_x, i_y] >= cvalues
                    if anticyclonic_search != acyc_not_cyc:
                        continue

                    # Find all pixels in the contour
                    i_x_in, i_y_in = current_contour.pixels_in(self)

                    # Maybe limit max must be replace with a maximum of surface
                    if current_contour.nb_pixel < pixel_limit[0] or current_contour.nb_pixel > pixel_limit[1]:
                        continue

                    # Compute amplitude
                    reset_centroid, amp = self.get_amplitude(current_contour, cvalues, data,
                                                             anticyclonic_search=anticyclonic_search,
                                                             level=self.contours.levels[corrected_coll_index], step=step)
                    # If we have a valid amplitude
                    if (not amp.within_amplitude_limits()) or (amp.amplitude == 0):
                        continue

                    if reset_centroid:

                        if self.is_circular():
                            centi = self.normalize_x_indice(reset_centroid[0])
                        else:
                            centi = reset_centroid[0]
                        centj = reset_centroid[1]
                        # To move in regular and unregular grid
                        if len(x.shape) == 1:
                            centlon_e = x[centi]
                            centlat_e = y[centj]
                        else:
                            centlon_e = x[centi, centj]
                            centlat_e = y[centi, centj]

                    # centlat_e and centlon_e must be index of maximum, we will loose some inner contour, if it's not
                    max_average_speed, speed_contour, inner_contour, speed_array, i_max_speed, i_inner = \
                        self.get_uavg(self.contours, centlon_e, centlat_e, current_contour, anticyclonic_search,
                                      corrected_coll_index)

                    # Use azimuth equal projection for radius
                    proj = Proj('+proj=aeqd +ellps=WGS84 +lat_0={1} +lon_0={0}'.format(*inner_contour.mean_coordinates))
                    # First, get position based on innermost
                    # contour
                    c_x, c_y = proj(inner_contour.lon, inner_contour.lat)
                    centx_s, centy_s, _, _ = fit_circle_c_numba(c_x, c_y)
                    centlon_s, centlat_s = proj(centx_s, centy_s, inverse=True)
                    # Second, get speed-based radius based on
                    # contour of max uavg
                    c_x, c_y = proj(speed_contour.lon, speed_contour.lat)
                    _, _, eddy_radius_s, aerr_s = fit_circle_c_numba(c_x, c_y)

                    # Instantiate new EddyObservation object (high cost need to be review)
                    properties = EddiesObservations(size=1, track_extra_variables=track_extra_variables,
                                                    track_array_variables=array_sampling,
                                                    array_variables=array_variables)

                    properties.obs['height_max_speed_contour'] = self.contours.cvalues[i_max_speed]
                    properties.obs['height_external_contour'] = cvalues
                    properties.obs['height_inner_contour'] = self.contours.cvalues[i_inner]
                    array_size = speed_array.shape[0]
                    properties.obs['nb_contour_selected'] = array_size
                    if speed_array.shape[0] == 1:
                        properties.obs['uavg_profile'][:] = speed_array[0]
                    else:
                        properties.obs['uavg_profile'] = raw_resample(speed_array, array_sampling)
                    properties.obs['amplitude'] = amp.amplitude
                    properties.obs['radius_s'] = eddy_radius_s / 1000
                    properties.obs['speed_radius'] = max_average_speed
                    properties.obs['radius_e'] = eddy_radius_e / 1000
                    properties.obs['shape_error_e'] = aerr
                    properties.obs['shape_error_s'] = aerr_s
                    properties.obs['lon'] = centlon_s
                    properties.obs['lat'] = centlat_s
                    properties.obs['contour_lon_e'], properties.obs['contour_lat_e'] = uniform_resample(
                        current_contour.lon, current_contour.lat, fixed_size=array_sampling)
                    properties.obs['contour_lon_s'], properties.obs['contour_lat_s'] = uniform_resample(
                            speed_contour.lon, speed_contour.lat, fixed_size=array_sampling)
                    if aerr > 99.9 or aerr_s > 99.9:
                        logging.warning('Strange shape at this step! shape_error : %f, %f', aerr, aerr_s)

                    eddies.append(properties)
                    # To reserve definitively the area
                    data.mask[i_x_in, i_y_in] = True
            if len(eddies) == 0:
                eddies_collection = EddiesObservations(track_extra_variables=track_extra_variables,
                                                       track_array_variables=array_sampling,
                                                       array_variables=array_variables)
            else:
                eddies_collection = EddiesObservations.concatenate(eddies)
            eddies_collection.sign_type = 1 if anticyclonic_search else -1
            eddies_collection.obs['time'] = (date - datetime(1950, 1, 1)).total_seconds() / 86400.

            # normalization longitude between 0 - 360, because storage have an offset on 180
            eddies_collection.obs['lon'] %= 360
            ref = eddies_collection.obs['lon'] - 180
            eddies_collection.obs['contour_lon_e'] = ((eddies_collection.obs['contour_lon_e'].T - ref) % 360 + ref).T
            eddies_collection.obs['contour_lon_s'] = ((eddies_collection.obs['contour_lon_s'].T - ref) % 360 + ref).T

            a_and_c.append(eddies_collection)
        h_units = self.units(grid_height)
        units = UnitRegistry()
        in_unit = units.parse_expression(h_units)
        if in_unit is not None:
            for name in ['amplitude', 'height_max_speed_contour', 'height_external_contour', 'height_inner_contour']:
                out_unit = units.parse_expression(VAR_DESCR[name]['nc_attr']['units'])
                factor, _ = in_unit.to(out_unit).to_tuple()
                a_and_c[0].obs[name] *= factor
                a_and_c[1].obs[name] *= factor
        return a_and_c

    def get_uavg(self, all_contours, centlon_e, centlat_e, original_contour, anticyclonic_search, level_start,
                 pixel_min=3):
        """
        Calculate geostrophic speed around successive contours
        Returns the average
        """
        max_average_speed = self.speed_coef_mean(original_contour)
        speed_array = [max_average_speed]
        pixel_min = 1

        eddy_contours = [original_contour]
        inner_contour = selected_contour = original_contour
        # Must start only on upper or lower contour, no need to test the two part
        step = 1 if anticyclonic_search else -1
        i_inner = i_max_speed = -1

        for i, coll in enumerate(all_contours.iter(start=level_start + step, step=step)):
            level_contour = coll.get_nearest_path_bbox_contain_pt(centlon_e, centlat_e)
            # Leave loop if no contours at level
            if level_contour is None:
                break
            # 1. Ensure polygon_i contains point centlon_e, centlat_e (Maybe we loose some inner contour if eddy
            #        core are not centered)
            # if winding_number_poly(centlon_e, centlat_e, level_contour.vertices) == 0:
            #     break
            # 2. Ensure polygon_i is within polygon_e
            if not poly_contain_poly(original_contour.vertices, level_contour.vertices):
                break
            # 3. Respect size range
            # nb_pixel properties need call of pixels_in before with a grid of pixel
            level_contour.pixels_in(self)
            if pixel_min > level_contour.nb_pixel:
                break
            # Interpolate uspd to seglon, seglat, then get mean
            level_average_speed = self.speed_coef_mean(level_contour)
            speed_array.append(level_average_speed)
            if level_average_speed >= max_average_speed:
                max_average_speed = level_average_speed
                i_max_speed = i
                selected_contour = level_contour
            inner_contour = level_contour
            eddy_contours.append(level_contour)
            i_inner = i
        for contour in eddy_contours:
            contour.used = True
        i_max_speed = level_start + step + step * i_max_speed
        i_inner = level_start + step + step * i_inner
        return max_average_speed, selected_contour, inner_contour, array(speed_array), i_max_speed, i_inner

    @staticmethod
    def _gaussian_filter(data, sigma, mode='reflect'):
        """Standard gaussian filter
        """
        local_data = data.copy()
        local_data[data.mask] = 0

        v = gaussian_filter(local_data, sigma=sigma, mode=mode)
        w = gaussian_filter(float_(~data.mask), sigma=sigma, mode=mode)

        with errstate(invalid='ignore'):
            return ma.array(v / w, mask=w == 0)

    @staticmethod
    def get_amplitude(contour, contour_height, data, anticyclonic_search=True, level=None, step=None):
        # Instantiate Amplitude object
        amp = Amplitude(
            # Indices of all pixels in contour
            contour=contour,
            # Height of level
            contour_height=contour_height,
            # All grid
            data=data,
            # Step by level
            interval=step)

        if anticyclonic_search:
            reset_centroid = amp.all_pixels_above_h0(level)
        else:
            reset_centroid = amp.all_pixels_below_h0(level)

        return reset_centroid, amp


class UnRegularGridDataset(GridDataset):
    """Class which manage unregular grid
    """

    __slots__ = (
        'index_interp',
        '_speed_norm',
        )

    def bbox_indice(self, vertices):
        dist, idx = self.index_interp.query(vertices, k=1)
        i_y = idx % self.x_c.shape[1]
        i_x = int_((idx - i_y) / self.x_c.shape[1])
        return (i_x.min() - self.N, i_x.max() + self.N + 1), (i_y.min() - self.N, i_y.max() + self.N + 1)

    def get_pixels_in(self, contour):
        (x_start, x_stop), (y_start, y_stop) = contour.bbox_slice
        pts = array((self.x_c[x_start:x_stop, y_start:x_stop].reshape(-1),
                     self.y_c[x_start:y_stop, y_start:y_stop].reshape(-1))).T
        mask = contour.contains_points(pts).reshape((x_stop - x_start, -1))
        i_x, i_y = where(mask)
        i_x += x_start
        i_y += y_start
        return i_x, i_y

    def normalize_x_indice(self, indices):
        """Not do"""
        return indices

    def nearest_grd_indice(self, x, y):
        dist, idx = self.index_interp.query((x, y), k=1)
        i_y = idx % self.x_c.shape[1]
        i_x = int_((idx - i_y) / self.x_c.shape[1])
        return i_x, i_y

    def compute_pixel_path(self, x0, y0, x1, y1):
        pass

    def init_pos_interpolator(self):
        logging.debug('Create a KdTree could be long ...')
        self.index_interp = cKDTree(
            column_stack((
                self.x_c.reshape(-1),
                self.y_c.reshape(-1)
            )))
        logging.debug('... OK')

    def _low_filter(self, grid_name, x_cut, y_cut, factor=40.):
        data = self.grid(grid_name)
        mean_data = data.mean()
        x = self.grid(self.coordinates[0])
        y = self.grid(self.coordinates[1])
        regrid_x_step = x_cut / factor
        regrid_y_step = y_cut / factor
        x_min, x_max, y_min, y_max = self.bounds
        x_array = arange(x_min, x_max + regrid_x_step, regrid_x_step)
        y_array = arange(y_min, y_max + regrid_y_step, regrid_y_step)
        bins = (x_array, y_array)

        x_flat, y_flat, z_flat = x.reshape((-1,)), y.reshape((-1,)), data.reshape((-1,))
        m = -z_flat.mask
        x_flat, y_flat, z_flat = x_flat[m], y_flat[m], z_flat[m]

        nb_value, bounds_x, bounds_y = histogram2d(
            x_flat, y_flat,
            bins=bins)

        sum_value, _, _ = histogram2d(
            x_flat, y_flat,
            bins=bins,
            weights=z_flat)

        with errstate(invalid='ignore'):
            z_grid = ma.array(sum_value / nb_value, mask=nb_value == 0)
        i_x, i_y = x_cut * 0.125 / regrid_x_step, y_cut * 0.125 / regrid_y_step
        m = nb_value == 0

        z_filtered = self._gaussian_filter(z_grid, (i_x, i_y))

        z_filtered[m] = 0
        x_center = (bounds_x[:-1] + bounds_x[1:]) / 2
        y_center = (bounds_y[:-1] + bounds_y[1:]) / 2
        opts_interpolation = dict(kx=1, ky=1, s=0)
        m_interp = RectBivariateSpline(x_center, y_center, m, **opts_interpolation)
        z_interp = RectBivariateSpline(x_center, y_center, z_filtered, **opts_interpolation).ev(x, y)
        return ma.array(z_interp, mask=m_interp.ev(x, y) > 0.00001)

    def speed_coef_mean(self, contour):
        dist, idx = self.index_interp.query(uniform_resample_stack(contour.vertices)[1:], k=4)
        i_y = idx % self.x_c.shape[1]
        i_x = int_((idx - i_y) / self.x_c.shape[1])
        # A simplified solution to be change by a weight mean
        return self._speed_norm[i_x, i_y].mean(axis=1).mean()

    def init_speed_coef(self, uname='u', vname='v'):
        self._speed_norm = (self.grid(uname) ** 2 + self.grid(vname) ** 2) ** .5


class RegularGridDataset(GridDataset):
    """Class only for regular grid
    """

    __slots__ = (
        '_speed_ev',
        '_is_circular',
        'x_size',
        '_x_step',
        '_y_step',
        )

    def __init__(self, *args, **kwargs):
        super(RegularGridDataset, self).__init__(*args, **kwargs)
        self._is_circular = None
        self.x_size = self.x_c.shape[0]
        self._x_step = (self.x_c[1:] - self.x_c[:-1]).mean()
        self._y_step = (self.y_c[1:] - self.y_c[:-1]).mean()

    def init_pos_interpolator(self):
        """Create function to have a quick index interpolator
        """
        self.xinterp = arange(self.x_bounds.shape[0])
        self.yinterp = arange(self.y_bounds.shape[0])

    def bbox_indice(self, vertices):
        return bbox_indice_regular(vertices, self.x_bounds[0], self.y_bounds[0], self.xstep, self.ystep, self.N)

    def get_pixels_in(self, contour):
        (x_start, x_stop), (y_start, y_stop) = contour.bbox_slice
        if x_stop < x_start:
            x_ref = contour.vertices[0, 0]
            x_array = (concatenate((self.x_c[x_start:], self.x_c[:x_stop])) - x_ref + 180) % 360 + x_ref -180
        else:
            x_array = self.x_c[x_start:x_stop]
        return winding_number_grid_in_poly(x_array, self.y_c[y_start:y_stop], x_start, x_stop, self.x_size, y_start, contour.vertices)


    def normalize_x_indice(self, indices):
        return indices % self.x_size

    def nearest_grd_indice(self, x, y):
        return int32(((x - self.x_bounds[0]) % 360) // self.xstep), \
               int32((y - self.y_bounds[0]) // self.ystep)

    @property
    def xstep(self):
        """Only for regular grid with no step variation
        """
        return self._x_step

    @property
    def ystep(self):
        """Only for regular grid with no step variation
        """
        return self._y_step

    def compute_pixel_path(self, x0, y0, x1, y1):
        """Give a series of index which describe the path between to position
        """
        # First x of grid
        x_ori = self.x_var[0]
        # Float index
        # ?? Normaly not callable
        f_x0 = self.xinterp((x0 - x_ori) % 360 + x_ori)
        f_x1 = self.xinterp((x1 - x_ori) % 360 + x_ori)
        f_y0 = self.yinterp(y0)
        f_y1 = self.yinterp(y1)
        # Int index
        i_x0, i_x1 = int32(round(f_x0)), int32(round(f_x1))
        i_y0, i_y1 = int32(round(f_y0)), int32(round(f_y1))

        # Delta index of x
        d_x = i_x1 - i_x0
        nb_x = self.x_var.shape[0] - 1
        d_x = (d_x + nb_x / 2) % nb_x - nb_x / 2
        i_x1 = i_x0 + d_x

        # Delta index of y
        d_y = i_y1 - i_y0

        d_max = maximum(abs(d_x), abs(d_y))

        # Compute number of pixel which we go trought
        nb_value = (abs(d_max) + 1).sum()
        # Create an empty array to store value of pixel across the travel
        # Max Index ~65000
        i_g = empty(nb_value, dtype='u2')
        j_g = empty(nb_value, dtype='u2')

        # Index to determine the position in the global array
        ii = 0
        # Iteration on each travel
        for i, delta in enumerate(d_max):
            # If the travel don't cross multiple pixel
            if delta == 0:
                i_g[ii: ii + delta + 1] = i_x0[i]
                j_g[ii: ii + delta + 1] = i_y0[i]
            # Vertical move
            elif d_x[i] == 0:
                sup = -1 if d_y[i] < 0 else 1
                i_g[ii: ii + delta + 1] = i_x0[i]
                j_g[ii: ii + delta + 1] = arange(i_y0[i], i_y1[i] + sup, sup)
            # Horizontal move
            elif d_y[i] == 0:
                sup = -1 if d_x[i] < 0 else 1
                i_g[ii: ii + delta + 1] = arange(i_x0[i], i_x1[i] + sup, sup) % nb_x
                j_g[ii: ii + delta + 1] = i_y0[i]
            # In case of multiple direction
            else:
                a = (i_x1[i] - i_x0[i]) / float(i_y1[i] - i_y0[i])
                if abs(d_x[i]) >= abs(d_y[i]):
                    sup = -1 if d_x[i] < 0 else 1
                    value = arange(i_x0[i], i_x1[i] + sup, sup)
                    i_g[ii: ii + delta + 1] = value % nb_x
                    j_g[ii: ii + delta + 1] = (value - i_x0[i]) / a + i_y0[i]
                else:
                    sup = -1 if d_y[i] < 0 else 1
                    j_g[ii: ii + delta + 1] = arange(i_y0[i], i_y1[i] + sup, sup)
                    i_g[ii: ii + delta + 1] = (int_(j_g[ii: ii + delta + 1]) - i_y0[i]) * a + i_x0[i]
            ii += delta + 1
        i_g %= nb_x
        return i_g, j_g, d_max

    def clean_land(self):
        """Function to remove all land pixel
        """
        pass

    def is_circular(self):
        """Check if grid is circular
        """
        if self._is_circular is None:
            self._is_circular = abs((self.x_bounds[0] % 360) - (self.x_bounds[-1] % 360)) < 0.0001
        return self._is_circular

    def kernel_lanczos(self, lat, wave_length, order=1):
        # Not really operational
        # wave_length in km
        # order must be int
        if order < 1:
            logging.warning('order must be superior to 0')
        order= ceil(order).astype(int)
        # Estimate size of kernel
        step_y_km = self.ystep * distance(0, 0, 0, 1) / 1000
        step_x_km = self.xstep * distance(0, lat, 1, lat) / 1000
        # half size will be multiply with by order
        half_x_pt, half_y_pt = ceil(wave_length / step_x_km).astype(int), ceil(wave_length / step_y_km).astype(int)

        y = arange(
            lat - self.ystep * half_y_pt * order,
            lat + self.ystep * half_y_pt * order + 0.01 * self.ystep,
            self.ystep)
        x = arange(
            -self.xstep * half_x_pt * order,
            self.xstep * half_x_pt * order + 0.01 * self.xstep,
            self.xstep)

        y, x = meshgrid(y, x)
        dist_norm = distance(0, lat, x, y) / 1000. / wave_length

        # sinc(d_x) and sinc(d_y) are windows and bessel function give an equivalent of sinc for lanczos filter
        kernel = sinc(dist_norm/order) * sinc(dist_norm)
        kernel[dist_norm > order] = 0
        return kernel

    def kernel_bessel(self, lat, wave_length, order=1):
        # wave_length in km
        # order must be int
        if order < 1:
            logging.warning('order must be superior to 0')
        order= ceil(order).astype(int)
        # Estimate size of kernel
        step_y_km = self.ystep * distance(0, 0, 0, 1) / 1000
        step_x_km = self.xstep * distance(0, lat, 1, lat) / 1000
        min_wave_length = max(step_x_km * 2, step_y_km * 2)
        if wave_length < min_wave_length:
            logging.error('Wave_length to short for resolution, must be > %d km', ceil(min_wave_length))
            raise Exception()
        # half size will be multiply with by order
        half_x_pt, half_y_pt = ceil(wave_length / step_x_km).astype(int), ceil(wave_length / step_y_km).astype(int)
        # x size is not good over 60 degrees
        y = arange(
            lat - self.ystep * half_y_pt * order,
            lat + self.ystep * half_y_pt * order + 0.01 * self.ystep,
            self.ystep)
        # We compute half + 1 and the other part will be compute by symetry
        x = arange(0, self.xstep * half_x_pt * order + 0.01 * self.xstep, self.xstep)
        y, x = meshgrid(y, x)
        dist_norm = distance(0, lat, x, y) / 1000. / wave_length
        # sinc(d_x) and sinc(d_y) are windows and bessel function give an equivalent of sinc for lanczos filter
        with errstate(invalid='ignore'):
            kernel = sinc(dist_norm/order) * j1(2 * pi * dist_norm) / dist_norm
        kernel[0, half_y_pt * order] = pi
        kernel[dist_norm > order] = 0
        # Symetry
        kernel_ = empty((half_x_pt * 2 * order + 1, half_y_pt * 2 * order + 1))
        kernel_[half_x_pt * order:] = kernel
        kernel_[:half_x_pt * order] = kernel[:0:-1]
        return kernel_

    def convolve_filter_with_dynamic_kernel(self, grid_name, kernel_func, lat_max=85, **kwargs_func):
        logging.warning('No filtering above %f degrees of latitude', lat_max)
        data = self.grid(grid_name).copy()
        # Matrix for result
        data_out = ma.empty(data.shape)
        data_out.mask = ones(data_out.shape, dtype=bool)
        for i, lat in enumerate(self.y_c):
            if abs(lat) > lat_max or data[:, i].mask.all():
                data_out.mask[:, i] = True
                continue
            # Get kernel
            kernel = kernel_func(lat, **kwargs_func)
            # Kernel shape
            k_shape = kernel.shape
            # Half size, k_shape must be always impair
            d_lat = int((k_shape[1] - 1) / 2)
            d_lon = int((k_shape[0] - 1) / 2)
            # Temporary matrix to have exact shape at outuput
            tmp_matrix = ma.zeros((2 * d_lon + data.shape[0], k_shape[1]))
            tmp_matrix.mask = ones(tmp_matrix.shape, dtype=bool)
            # Slice to apply on input data
            sl_lat_data = slice(max(0, i - d_lat), min(i + d_lat, data.shape[1]))
            # slice to apply on temporary matrix to store input data
            sl_lat_in = slice(d_lat - (i - sl_lat_data.start), d_lat + (sl_lat_data.stop - i))
            # If global => manual wrapping
            if self.is_circular():
                tmp_matrix[:d_lon, sl_lat_in] = data[-d_lon:, sl_lat_data]
                tmp_matrix[-d_lon:, sl_lat_in] = data[:d_lon, sl_lat_data]
            # Copy data
            tmp_matrix[d_lon:-d_lon, sl_lat_in] = data[:, sl_lat_data]
            # Convolution
            m = ~tmp_matrix.mask
            tmp_matrix[~m] = 0

            demi_x, demi_y = k_shape[0] // 2, k_shape[1] // 2
            values_sum = filter2D(tmp_matrix.data, -1, kernel)[demi_x:-demi_x, demi_y]
            kernel_sum = filter2D(m.astype(float), -1, kernel)[demi_x:-demi_x, demi_y]
            with errstate(invalid='ignore'):
                data_out[:, i] = values_sum / kernel_sum
        data_out = ma.array(data_out, mask=data.mask + data_out.mask)
        return data_out

    def _low_filter(self, grid_name, x_cut, y_cut):
        """low filtering
        """
        i_x, i_y = x_cut * 0.125 / self.xstep, y_cut * 0.125 / self.xstep
        logging.info(
            'Filtering with this wave : (%s, %s) converted in pixel (%s, %s)',
            x_cut, y_cut, i_x, i_y
        )
        data = self.grid(grid_name).copy()
        data[data.mask] = 0
        return self._gaussian_filter(
            data,
            (i_x, i_y),
            mode='wrap' if self.is_circular() else 'reflect')

    def lanczos_high_filter(self, grid_name, wave_length, order=1, lat_max=85):
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_lanczos, lat_max=lat_max, wave_length=wave_length, order=order)
        self.vars[grid_name] -= data_out

    def lanczos_low_filter(self, grid_name, wave_length, order=1, lat_max=85):
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_lanczos, lat_max=lat_max, wave_length=wave_length, order=order)
        self.vars[grid_name] = data_out

    def bessel_band_filter(self, grid_name, wave_length_inf, wave_length_sup, **kwargs):
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_bessel, wave_length=wave_length_inf, **kwargs)
        self.vars[grid_name] = data_out
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_bessel, wave_length=wave_length_sup, **kwargs)
        self.vars[grid_name] -= data_out

    def bessel_high_filter(self, grid_name, wave_length, order=1, lat_max=85):
        logging.debug('Run filtering with wave of %(wave_length)s km and order of %(order)s ...',
                      dict(wave_length=wave_length, order=order))
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_bessel, lat_max=lat_max, wave_length=wave_length, order=order)
        logging.debug('Filtering done')
        self.vars[grid_name] -= data_out

    def bessel_low_filter(self, grid_name, wave_length, order=1, lat_max=85):
        data_out = self.convolve_filter_with_dynamic_kernel(
            grid_name, self.kernel_bessel, lat_max=lat_max, wave_length=wave_length, order=order)
        self.vars[grid_name] = data_out

    def spectrum_lonlat(self, grid_name, area=None, ref=None, **kwargs):
        if area is None:
            area = dict(llcrnrlon=190, urcrnrlon=280, llcrnrlat=-62, urcrnrlat=8)
        scaling = kwargs.pop('scaling', 'density')
        x0, y0 = self.nearest_grd_indice(area['llcrnrlon'], area['llcrnrlat'])
        x1, y1 = self.nearest_grd_indice(area['urcrnrlon'], area['urcrnrlat'])

        data = self.grid(grid_name)[x0:x1,y0:y1]

        # Lat spectrum
        pws = list()
        step_y_km = self.ystep * distance(0, 0, 0, 1) / 1000
        nb_invalid = 0
        for i, _ in enumerate(self.x_c[x0:x1]):
            f, pw = welch(data[i,:],  1 / step_y_km, scaling=scaling, **kwargs)
            if isnan(pw).any():
                nb_invalid += 1
                continue
            pws.append(pw)
        if nb_invalid:
            logging.warning('%d/%d columns invalid', nb_invalid, i + 1)
        lat_content = 1 / f, array(pws).mean(axis=0)

        # Lon spectrum
        fs, pws = list(), list()
        f_min, f_max = None, None
        nb_invalid = 0
        for i, lat in enumerate(self.y_c[y0:y1]):
            step_x_km = self.xstep * distance(0, lat, 1, lat) / 1000
            f, pw = welch(data[:,i], 1 / step_x_km, scaling=scaling, **kwargs)
            if isnan(pw).any():
                nb_invalid += 1
                continue
            if f_min is None:
                f_min = f.min()
                f_max = f.max()
            else:
                f_min = max(f_min, f.min())
                f_max = min(f_max, f.max())
            fs.append(f)
            pws.append(pw)
        if nb_invalid:
            logging.warning('%d/%d lines invalid', nb_invalid, i + 1)
        f_interp = linspace(f_min, f_max, f.shape[0])
        pw_m = array(
            [interp1d(f, pw, fill_value=0., bounds_error=False)(f_interp) for f, pw in zip(fs, pws)]).mean(axis=0)
        lon_content = 1 / f_interp, pw_m
        if ref is None:
            return lon_content, lat_content
        else:
            ref_lon_content, ref_lat_content = ref.spectrum_lonlat(grid_name, area, **kwargs)
            return (lon_content[0], lon_content[1] / ref_lon_content[1]), \
                   (lat_content[0], lat_content[1] / ref_lat_content[1])

    def add_uv(self, grid_height):
        """Compute a u and v grid
        """
        h_dict = self.variables_description[grid_height]
        for variable in ('u', 'v'):
            self.variables_description[variable] = dict(
                infos=h_dict['infos'].copy(),
                attrs=h_dict['attrs'].copy(),
                args=tuple((variable, *h_dict['args'][1:])),
                kwargs=h_dict['kwargs'].copy(),
            )
            self.variables_description[variable]['attrs']['units'] += '/s'
        data = self.grid(grid_height)
        gof = sin(deg2rad(self.y_c)) * ones((self.x_c.shape[0], 1)) * 4. * pi / 86400.
        # gof = sin(deg2rad(self.y_c))* ones((self.x_c.shape[0], 1))  * 4. * pi / (23 * 3600 + 56 *60 +4.1 )
        with errstate(divide='ignore'):
            gof = self.GRAVITY / (gof * ones((self.x_c.shape[0], 1)))

        m_y = array((1, 0, -1))
        m_x = array((1, 0, -1))
        d_hy = convolve(
            data,
            weights=m_y.reshape((-1, 1)).T
        )
        mask = convolve(
            int8(data.mask),
            weights=ones(m_y.shape).reshape((1, -1))
        )
        d_hy = ma.array(d_hy, mask=mask != 0)

        d_y = self.EARTH_RADIUS * 2 * pi / 360 * convolve(self.y_c, m_y)

        self.vars['u'] = - d_hy / d_y * gof
        mode = 'wrap' if self.is_circular() else 'reflect'
        d_hx = convolve(
            data,
            weights=m_x.reshape((-1, 1)),
            mode=mode,
        )
        mask = convolve(
            int8(data.mask),
            weights=ones(m_x.shape).reshape((-1, 1)),
            mode=mode,
        )
        d_hx = ma.array(d_hx, mask=mask != 0)
        d_x_degrees = convolve(self.x_c, m_x, mode=mode).reshape((-1, 1))
        d_x_degrees = (d_x_degrees + 180) % 360 - 180
        d_x = self.EARTH_RADIUS * 2 * pi / 360 * d_x_degrees * cos(deg2rad(self.y_c))
        self.vars['v'] = d_hx / d_x * gof

    def speed_coef_mean(self, contour):
        """some nan can be compute over contour if we are near border,
        something to explore
        """
        return mean_on_regular_contour(
            self.x_c, self.y_c,
            self._speed_ev, self._speed_ev.mask,
            contour.vertices)

    def init_speed_coef(self, uname='u', vname='v'):
        """Draft
        """
        self._speed_ev = (self.grid(uname) ** 2 + self.grid(vname) ** 2) ** .5

    def display(self, ax, name, **kwargs):
        if 'cmap' not in kwargs:
            kwargs['cmap'] = 'coolwarm'
        return ax.pcolormesh(self.x_bounds, self.y_bounds, self.grid(name).T, **kwargs)

    def interp(self, grid_name, lons, lats):
        """
        Compute z over lons, lats
        Args:
            grid_name: Grid which will be interp
            lons: new x
            lats: new y

        Returns:
            new z
        """
        g = self.grid(grid_name)
        return interp2d_geo(self.x_c, self.y_c, g, g.mask, lons, lats)


@njit(cache=True, fastmath=True, parallel=True)
def custom_convolution(data, mask, kernel):
    """do sortin at high lattitude big part of value are masked"""
    nb_x = kernel.shape[0]
    demi_x = int((nb_x - 1) / 2)
    demi_y = int((kernel.shape[1] - 1) / 2)
    out = empty(data.shape[0] - nb_x + 1)
    for i in prange(out.shape[0]):
        if mask[i + demi_x, demi_y] == 1:
            w = (mask[i:i + nb_x] * kernel).sum()
            if w != 0:
                out[i] = (data[i:i + nb_x] * kernel).sum() / w
            else:
                out[i] = nan
        else:
            out[i] = nan
    return out


@njit(cache=True, fastmath=True)
def interp2d_geo(x_g, y_g, z_g, m_g, x, y):
    """For geographic grid, test of cicularity
    Maybe test if we are out of bounds
    """
    x_ref = x_g[0]
    y_ref = y_g[0]
    x_step = x_g[1] - x_ref
    y_step = y_g[1] - y_ref
    nb_x = x_g.shape[0]
    is_circular = (x_g[-1] + x_step) % 360 == x_g[0] % 360
    z = empty(x.shape)
    for i in prange(x.size):
        x_ = (x[i] - x_ref) / x_step
        y_ = (y[i] - y_ref) / y_step
        i0 = int(floor(x_))
        i1 = i0 + 1
        if is_circular:
            xd = (x_ - i0)
            i0 %= nb_x
            i1 %= nb_x
        j0 = int(floor(y_))
        j1 = j0 + 1
        yd = (y_ - j0)
        z00 = z_g[i0, j0]
        z01 = z_g[i0, j1]
        z10 = z_g[i1, j0]
        z11 = z_g[i1, j1]
        if m_g[i0, j0] or m_g[i0, j1] or m_g[i1, j0] or m_g[i1, j1]:
            z[i] = nan
        else:
            z[i] = (z00 * (1 - xd) + (z10 * xd)) * (1 - yd) + (z01 * (1 - xd) + z11 * xd) * yd
    return z


@njit(cache=True)
def bbox_indice_regular(vertices, x0, y0, xstep, ystep, N):
    lon, lat = vertices[:,0], vertices[:,1]
    lon_min, lon_max = lon.min(), lon.max()
    lat_min, lat_max = lat.min(), lat.max()
    i_x0, i_y0 = int32(((lon_min - x0) % 360) // xstep), int32((lat_min - y0) // ystep)
    i_x1, i_y1 = int32(((lon_max - x0) % 360) // xstep), int32((lat_max - y0) // ystep)
    slice_x = i_x0 - N, i_x1 + N + 1
    slice_y = i_y0 - N, i_y1 + N + 1
    return slice_x, slice_y