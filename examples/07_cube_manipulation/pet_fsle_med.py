"""
FSLE experiment in med
======================

Example to build FSLE, parameter values must be adapted for your case.

Example use a method similar to `AVISO flse`_

.. _AVISO flse:
    https://www.aviso.altimetry.fr/en/data/products/value-added-products/
    fsle-finite-size-lyapunov-exponents/fsle-description.html

"""

from matplotlib import pyplot as plt
from numba import njit
from numpy import arange, empty, isnan, log2, ma, meshgrid, zeros

from py_eddy_tracker import start_logger
from py_eddy_tracker.data import get_path
from py_eddy_tracker.dataset.grid import GridCollection, RegularGridDataset

start_logger().setLevel("ERROR")


# %%
# ADT in med
# ---------------------
c = GridCollection.from_netcdf_cube(
    get_path("dt_med_allsat_phy_l4_2005T2.nc"),
    "longitude",
    "latitude",
    "time",
    heigth="adt",
)


# %%
# Methods to compute fsle
# -----------------------
@njit(cache=True, fastmath=True)
def check_p(x, y, g, m, dt, dist_init=0.02, dist_max=0.6):
    """
    Check if distance between eastern or northern particle to center particle is bigger than `dist_max`
    """
    nb_p = x.shape[0] // 3
    delta = dist_max ** 2
    for i in range(nb_p):
        i0 = i * 3
        i_n = i0 + 1
        i_e = i0 + 2
        # If particle already set, we skip
        if m[i0] or m[i_n] or m[i_e]:
            continue
        # Distance with north
        dxn, dyn = x[i0] - x[i_n], y[i0] - y[i_n]
        dn = dxn ** 2 + dyn ** 2
        # Distance with east
        dxe, dye = x[i0] - x[i_e], y[i0] - y[i_e]
        de = dxe ** 2 + dye ** 2

        if dn >= delta or de >= delta:
            s1 = dxe ** 2 + dxn ** 2 + dye ** 2 + dyn ** 2
            s2 = ((dxn + dye) ** 2 + (dxe - dyn) ** 2) * (
                (dxn - dye) ** 2 + (dxe + dyn) ** 2
            )
            g[i] = 1 / (2 * dt) * log2(1 / (2 * dist_init ** 2) * (s1 + s2 ** 0.5))
            m[i0], m[i_n], m[i_e] = True, True, True


@njit(cache=True)
def build_triplet(x, y, step=0.02):
    """
    Triplet building for each position we add east and north point with defined step
    """
    nb_x = x.shape[0]
    x_ = empty(nb_x * 3, dtype=x.dtype)
    y_ = empty(nb_x * 3, dtype=y.dtype)
    for i in range(nb_x):
        i0 = i * 3
        i_n, i_e = i0 + 1, i0 + 2
        x__, y__ = x[i], y[i]
        x_[i0], y_[i0] = x__, y__
        x_[i_n], y_[i_n] = x__, y__ + step
        x_[i_e], y_[i_e] = x__ + step, y__
    return x_, y_


# %%
# Particles
# ---------
step = 0.02
t0 = 20268
x0_, y0_ = -5, 30
lon_p, lat_p = arange(x0_, x0_ + 43, step), arange(y0_, y0_ + 16, step)
x0, y0 = meshgrid(lon_p, lat_p)
grid_shape = x0.shape
x0, y0 = x0.reshape(-1), y0.reshape(-1)
# Identify all particle not on land
m = ~isnan(c[t0].interp("adt", x0, y0))
x0, y0 = x0[m], y0[m]

# %%
# FSLE
# ----
time_step_by_days = 5
# Array to compute fsle
fsle = zeros(x0.shape[0], dtype="f4")
x, y = build_triplet(x0, y0)
used = zeros(x.shape[0], dtype="bool")

# advection generator
kw = dict(t_init=t0, nb_step=1, backward=True, mask_particule=used)
p = c.advect(x, y, "u", "v", time_step=86400 / time_step_by_days, **kw)

nb_days = 85
# We check at each step of advection if particle distance is over `dist_max`
for i in range(time_step_by_days * nb_days):
    t, xt, yt = p.__next__()
    dt = t / 86400.0 - t0
    check_p(xt, yt, fsle, used, dt, dist_max=0.2, dist_init=step)

# Get index with original_position
i = ((x0 - x0_) / step).astype("i4")
j = ((y0 - y0_) / step).astype("i4")
fsle_ = empty(grid_shape, dtype="f4")
used_ = zeros(grid_shape, dtype="bool")
fsle_[j, i] = fsle
used_[j, i] = used[::3]
# Create a grid object
fsle_custom = RegularGridDataset.with_array(
    coordinates=("lon", "lat"),
    datas=dict(
        fsle=ma.array(fsle_.T, mask=~used_.T),
        lon=lon_p,
        lat=lat_p,
    ),
    centered=True,
)

# %%
# Display FSLE
# ------------
fig = plt.figure(figsize=(13, 5), dpi=150)
ax = fig.add_axes([0.03, 0.03, 0.90, 0.94])
ax.set_xlim(-6, 36.5), ax.set_ylim(30, 46)
ax.set_aspect("equal")
ax.set_title("Finite size lyapunov exponent", weight="bold")
kw = dict(cmap="viridis_r", vmin=-15, vmax=0)
m = fsle_custom.display(ax, 1 / fsle_custom.grid("fsle"), **kw)
ax.grid()
cb = plt.colorbar(m, cax=fig.add_axes([0.94, 0.05, 0.01, 0.9]))