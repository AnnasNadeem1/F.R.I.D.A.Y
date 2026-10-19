import os

# Math libraries (OpenBLAS via numpy/scipy) otherwise reserve a scratch buffer per CPU
# thread: ~500 MB each time they load on a 16-thread CPU. The models here are small and
# set their own thread counts, so four is plenty. Must run before numpy is imported.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS"):
    os.environ.setdefault(_var, "4")
