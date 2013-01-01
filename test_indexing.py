import numpy as np

from parakeet import each
from testing_helpers import eq, expect, run_local_ts

shape_1d = 40
ints_1d = np.arange(shape_1d)
floats_1d = np.arange(shape_1d, dtype='float')
bools_1d = ints_1d % 2

vecs = [ints_1d, floats_1d, bools_1d]

shape_2d = (4,10)
matrices = [np.reshape(vec, shape_2d) for vec in vecs]

shape_3d = (4,5,2)
tensors = [np.reshape(mat, shape_3d) for mat in matrices]

def index_1d(x, i):
  return x[i]

def test_index_1d():
  for vec in vecs:
    expect(index_1d, [vec, 20], vec[20])

def index_2d(x, i, j):
  return x[i, j]

def test_index_2d():
  for mat in matrices:
    expect(index_2d, [mat, 2, 5], mat[2,5])

def index_3d(x, i, j, k):
  return x[i, j, k]

def test_index_3d():
  for x in tensors:
    expect(index_3d, [x, 2, 2, 1], x[2,2,1])

def set_idx_1d(arr,i,val):
  arr[i] = val
  return arr

def test_set_idx_1d():
  idx = 10
  for vec in vecs:
    vec1, vec2 = vec.copy(), vec.copy()
    val = -vec[idx]
    vec2[idx] = val
    expect(set_idx_1d, [vec1, idx, val], vec2)

def set_idx_2d(arr,i,j,val):
  arr[i, j] = val
  return arr

def test_set_idx_2d():
  i = 2
  j = 2
  for mat in matrices:
    mat1, mat2 = mat.copy(), mat.copy()
    val = -mat[i,j]
    mat2[i,j] = val
    expect(set_idx_2d, [mat1, i, j, val], mat2)

def set_idx_3d(arr, i, j, k, val):
  arr[i, j, k] = val
  return arr

def test_set_idx_3d():
  i = 2
  j = 3
  k = 1
  for x in tensors:
    x1, x2 = x.copy(), x.copy()
    val = -x[i, j, k]
    x2[i, j, k] = val
    expect(set_idx_3d, [x1, i, j, k, val], x2)

def bool_idx(X, a, i):
  return X[a == i]

def test_bool_idx():
  assign = np.random.randint(3, shape_1d)
  idxs = np.arange(3)
  def run_bool_idx(i):
    return bool_idx(ints_1d, assign, i)
  par_rslt = each(run_bool_idx, idxs)
  py_rslt = np.array(map(run_bool_idx, idxs))
  assert eq(par_rslt, py_rslt), "Expected %s got %s" % (py_rslt, par_rslt)

if __name__ == '__main__':
  run_local_ts()
