import array_type
import syntax
import syntax_helpers
import tuple_type

from core_types import Int64
from inline import do_inline 
from syntax_helpers import zero_i64, one_i64
from transform import Transform

class LowerTiledAdverbs(Transform):
  default_reg_tile_size = 4

  def __init__(self, nesting_idx = -1, fixed_idx = -1):
    Transform.__init__(self)
    self.nesting_idx = nesting_idx
    self.fixed_idx = fixed_idx
    self.tiling = False
    self.tile_sizes_param = None
    self.fixed_tile_sizes = None
    self.dl_tile_estimates = []
    self.ml_tile_estimates = []
    
    self.output_locations = []
    
    # For now, we'll assume that no closure variables have the same name.
    self.closure_vars = {}

  # TODO: Change this to be able to accept fixed tile sizes properly
  def pre_apply(self, fn, fixed_tile_sizes = None):
    if not self.tile_sizes_param:
      tile_type = \
          tuple_type.make_tuple_type([Int64 for _ in range(fn.num_tiles)])
      self.tile_sizes_param = self.fresh_var(tile_type, "tile_params")
    if self.fixed_tile_sizes == None:
      self.fixed_tile_sizes = \
          [LowerTiledAdverbs.default_reg_tile_size] * fn.num_tiles
    return fn

  def get_closure_arg(self, closure_elt):
    if isinstance(closure_elt, syntax.ClosureElt):
      if isinstance(closure_elt.closure, syntax.Closure):
        return closure_elt.closure.args[closure_elt.index]
      elif isinstance(closure_elt.closure, syntax.Var):
        closure = self.closure_vars[closure_elt.closure.name]
        return closure.args[closure_elt.index]
      else:
        assert False, "Unknown closure type for closure elt %s" % closure_elt
    elif isinstance(closure_elt, syntax.Var):
      return closure_elt
    else:
      assert False, "Unknown closure closure elt type %s" % closure_elt

  def transform_Assign(self, stmt):
    if isinstance(stmt.rhs, syntax.Closure):
      self.closure_vars[stmt.lhs.name] = stmt.rhs
    stmt.rhs = self.transform_expr(stmt.rhs)
    stmt.lhs = self.transform_lhs(stmt.lhs)
    return stmt

  def transform_TypedFn(self, expr):
    nested_lower = LowerTiledAdverbs(nesting_idx = self.nesting_idx,
                                     fixed_idx = self.fixed_idx)
    nested_lower.tile_sizes_param = self.tile_sizes_param
    nested_lower.fixed_tile_sizes = self.fixed_tile_sizes
    return nested_lower.apply(expr)

  def transform_TiledMap(self, expr):
    args = expr.args
    axes = expr.axes

    # TODO: Should make sure that all the shapes conform here,
    # but we don't yet have anything like assertions or error handling
    niters = self.shape(args[0], syntax_helpers.unwrap_constant(axes[0]))

    # Create the tile size variable and find the number of tiles
    if expr.fixed_tile_size:
      self.fixed_idx += 1
      tile_size = self.fixed_tile_sizes[self.fixed_idx]
    else:
      self.tiling = True
      self.fn.has_tiles = True
      self.nesting_idx += 1
      tile_size = self.index(self.tile_sizes_param, self.nesting_idx)
    fn = self.transform_expr(expr.fn)

    inner_fn = self.get_fn(fn)
    nested_has_tiles = inner_fn.has_tiles

    # Increase the nesting_idx by the number of tiles in the nested fn
    self.nesting_idx += inner_fn.num_tiles

    slice_t = array_type.make_slice_type(Int64, Int64, Int64)

    closure_args = [self.get_closure_arg(e)
                    for e in self.closure_elts(fn)]
    output_args = closure_args + args
    
    if nested_has_tiles:
      n_tile_sizes = len(self.tile_sizes_param.type.elt_types)
      ones = self.tuple([syntax_helpers.one_i64] * n_tile_sizes)
      output_args.append(ones)      
    array_result = self._create_output_array(inner_fn, output_args, [], 
                                             "array_result")
    
    i = self.fresh_var(niters.type, "i")
    start = zero_i64 
    stop = niters 
    
    self.blocks.push()
    slice_stop = self.add(i, tile_size, "slice_stop")
    slice_stop_min = self.min(slice_stop, niters, "slice_stop_min")
    # Take care of stragglers via checking bound every iteration.
    
    tile_bounds = syntax.Slice(i, slice_stop_min, one_i64, type=slice_t)
    nested_args = [self.index_along_axis(arg, axis, tile_bounds)
                   for arg, axis in zip(args, axes)]
    out_idx = self.nesting_idx if self.tiling else self.fixed_idx
    output_region = self.index_along_axis(array_result, out_idx, tile_bounds)
    if nested_has_tiles:
      nested_args.append(self.tile_sizes_param)
    body = self.blocks.pop()
    do_inline(inner_fn, 
              closure_args + nested_args, 
              self.type_env, 
              body, 
              result_var = output_region)
    self.blocks += syntax.ForLoop(i, start, stop, one_i64, body, {})
    return array_result

  def transform_TiledReduce(self, expr):
    args = expr.args
    axes = expr.axes

    # TODO: Should make sure that all the shapes conform here,
    # but we don't yet have anything like assertions or error handling.
    niters = self.shape(args[0], syntax_helpers.unwrap_constant(axes[0]))

    if expr.fixed_tile_size:
      self.fixed_idx += 1
      tile_size = self.fixed_tile_sizes[self.fixed_idx]
    else:
      self.tiling = True
      self.fn.has_tiles = True
      self.nesting_idx += 1
      tile_size = self.index(self.tile_sizes_param, self.nesting_idx)

    slice_t = array_type.make_slice_type(Int64, Int64, Int64)
    callable_fn = self.transform_expr(expr.fn)
    inner_fn = self.get_fn(callable_fn)
    callable_combine = self.transform_expr(expr.combine)
    inner_combine = self.get_fn(callable_combine)

    closure_args = [self.get_closure_arg(e)
                    for e in self.closure_elts(callable_fn)]
    output_args = closure_args + args
    loop_rslt = self._create_output_array(inner_fn, output_args, [],
                                          "loop_result")
    init = loop_rslt
    rslt_t = loop_rslt.type
    not_array = array_type.get_rank(rslt_t) == 0
    if not_array:
      loop_before = self.fresh_var(rslt_t, "loop_before")
      init = loop_before

    # Lift the initial value and fill it.
    def init_unpack(i, cur):
      if i == 0:
        return syntax.Assign(cur, expr.init)
      else:
        j, j_after, merge = self.loop_counter("j")
        init_cond = self.lt(j, self.shape(cur, 0))
        self.blocks.push()
        n = self.index_along_axis(cur, 0, j)
        self.blocks += init_unpack(i-1, n)
        self.assign(j_after, self.add(j, syntax_helpers.one_i64))
        body = self.blocks.pop()
        return syntax.While(init_cond, body, merge)
    num_exps = array_type.get_rank(init.type) - \
               array_type.get_rank(expr.init.type)
    self.blocks += init_unpack(num_exps, init)

    # Loop over the remaining tiles.
    rslt_tmp = self.fresh_var(rslt_t, "rslt_tmp")
    i, i_after, merge = self.loop_counter("i")
    loop_cond = self.lt(i, niters)
    if not_array:
      loop_after = self.fresh_var(rslt_t, "loop_after")
      merge[loop_rslt.name] = (loop_before, loop_after)

    self.blocks.push()
    # Take care of stragglers via checking bound every iteration.
    next_bound = self.add(i, tile_size, "next_bound")
    tile_cond = self.lte(next_bound, niters)
    tile_merge = {i_after.name:(next_bound, niters)}
    self.blocks += syntax.If(tile_cond, [], [], tile_merge)

    tile_bounds = syntax.Slice(i, i_after, syntax_helpers.one(Int64),
                               type=slice_t)
    nested_args = [self.index_along_axis(arg, axis, tile_bounds)
                   for arg, axis in zip(args, axes)]
    nested_call = syntax.Call(callable_fn, nested_args,
                              type=inner_fn.return_type)
    self.assign(rslt_tmp, nested_call)
    nested_combine = syntax.Call(callable_combine,
                                 [loop_rslt, rslt_tmp],
                                 type=inner_combine.return_type)
    if not_array:
      self.assign(loop_after, nested_combine)
    else:
      outidx = \
          self.tuple([syntax_helpers.slice_none] * loop_rslt.type.rank)
      self.setidx(loop_rslt, outidx, nested_combine)
    loop_body = self.blocks.pop()
    self.blocks += syntax.While(loop_cond, loop_body, merge)

    return loop_rslt

  def post_apply(self, fn):
    print fn 
    if self.tiling:
      fn.arg_names.append(self.tile_sizes_param.name)
      fn.input_types += (self.tile_sizes_param.type,)
      fn.type_env[self.tile_sizes_param.name] = self.tile_sizes_param.type
    return fn
