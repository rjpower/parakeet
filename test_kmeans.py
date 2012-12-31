import numpy as np
import parakeet

from testing_helpers import eq, run_local_tests

def ident(x):
  return x

def sqr_dist(x, y):
  return sum((x-y) * (x-y))

def argmin((curmin, curminidx), (val, idx)):
  if val < curmin:
    return (val, idx)
  else:
    return (curmin, curminidx)

py_reduce = reduce
inf = float("inf")
def py_minidx(C, idxs, x):
  def run_sqr_dist(c):
    return sqr_dist(c, x)
  dists = map(run_sqr_dist, C)
  def z(x, y):
    return (x, y)
  zipped = zip(dists, idxs)
  return py_reduce(argmin, zipped, (inf,-1))[1]

def calc_centroid(X, a, i):
  cur_members = X[a == i]
  num_members = cur_members.shape[0]
  if num_members == 0:
    return 0.0
  else:
    return sum(cur_members) / num_members

def kmeans(X, assign, k):
  idxs = np.arange(k)

  def run_calc_centroid(i):
    return calc_centroid(X, assign, i)

  C = map(run_calc_centroid, idxs)
  converged = False
  while not converged:
    lastAssign = assign
    def run_py_minidx(x):
      return py_minidx(C, idxs, x)
    assign = np.array(map(run_py_minidx, X))
    converged = np.all(assign == lastAssign)
    C = map(run_calc_centroid, idxs)
  return C, assign

from parakeet import each, reduce
def par_minidx(C, idxs, x):
  def run_sqr_dist(c):
    return sqr_dist(c, x)
  dists = each(run_sqr_dist, C)
  def z(x, y):
    return (x, y)
  zipped = each(z, dists, idxs)
  return reduce(argmin, zipped, init=(inf,-1))[1]

def par_kmeans(X, assign, k):
  idxs = np.arange(k)

  def run_calc_centroid(i):
    return calc_centroid(X, assign, i)

  #C = parakeet.each(run_calc_centroid, idxs)
  C = map(run_calc_centroid, idxs)
  converged = False
  while not converged:
    lastAssign = assign
    def run_minidx(x):
      return par_minidx(C, idxs, x)
    assign = parakeet.each(run_minidx, X)
    converged = np.all(assign == lastAssign)
    #C = parakeet.each(run_calc_centroid, idxs)
    C = map(run_calc_centroid, idxs)
  return C, assign

def test_kmeans():
  s = 10
  n = 50
  X = np.random.randn(n*s).reshape(n,s)
  assign = np.random.randint(2, size=n)
  print assign
  k = 2
  C, assign = kmeans(X, assign, k)
  print C
  print assign
  par_C, par_assign = par_kmeans(X, assign, k)
  print par_C
  print par_assign

if __name__ == '__main__':
  run_local_tests()
