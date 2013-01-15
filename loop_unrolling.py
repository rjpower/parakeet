import names 
import syntax_helpers 

from array_type import ArrayT
from clone_function import CloneFunction
from collect_vars import  collect_binding_names
from offset_analysis import OffsetAnalysis
from syntax import Assign, ForLoop, While, If, Return  
from syntax import Const, Var     
from tuple_type import TupleT
from transform import Transform

def simple_assignment_type(t):
  if t.__class__ is TupleT:
    return all(simple_assignment_type(elt_t) for elt_t in t.elt_types)
  else:
    return t.__class__ is not ArrayT

def simple_loop_body(stmts):
  for stmt in stmts:
    if stmt.__class__ in (Return, While, ForLoop):
      return False
    elif stmt.__class__ is If:
      if not simple_loop_body(stmt.true) or not simple_loop_body(stmt.false):
        return False
    elif stmt.__class__ is Assign and \
         not simple_assignment_type(stmt.lhs.type):
      return False
  return True

def count_nested_stmts(stmt):
  if stmt.__class__ is If:
    return count_stmts(stmt.true) + count_stmts(stmt.false)
  elif stmt.__class__ is ForLoop or stmt.__class__ is While:
    return count_stmts(stmt.body)
  else:
    return 0
  
def count_stmts(stmts):
  return len(stmts) + sum(count_nested_stmts(stmt) for stmt in stmts)
  

class CloneStmt(CloneFunction):
  def __init__(self, outer_type_env):
    Transform.__init__(self)
    self.recursive = False
    self.type_env = outer_type_env
    self.rename_dict = {}

  def rename(self, old_name):
    old_type = self.type_env[old_name]
    new_name = names.refresh(old_name)
    new_var = Var(new_name, old_type)
    self.rename_dict[old_name] = new_var
    self.type_env[new_name] = old_type
    return new_name

  def rename_var(self, old_var):
    new_name = names.refresh(old_var.name)
    new_var = Var(new_name, old_var.type)
    self.rename_dict[old_var.name] = new_var
    self.type_env[new_name] = old_var.type
    return new_var

  def transform_merge(self, merge):
    new_merge = {}
    for (old_name, (l,r)) in merge.iteritems():
      new_name = self.rename(old_name)
      new_left = self.transform_expr(l)
      new_right = self.transform_expr(r)
      new_merge[new_name] = (new_left, new_right)
    return new_merge

  def transform_merge_before_loop(self, merge):
    new_merge = {}
    for (old_name, (l,r)) in merge.iteritems():
      new_name = self.rename(old_name)

      new_left = self.transform_expr(l)
      new_merge[new_name] = (new_left, r)
    return new_merge

  def transform_merge_after_loop(self, merge):
    for (new_name, (new_left, old_right)) in merge.items():
      merge[new_name] = (new_left, self.transform_expr(old_right))
    return merge

  def transform_Assign(self, expr):
    for name in collect_binding_names(expr.lhs):
      self.rename(name)
    new_lhs = self.transform_expr(expr.lhs)
    new_rhs = self.transform_expr(expr.rhs)

    return Assign(new_lhs, new_rhs)

  def transform_Var(self, expr):
    return self.rename_dict.get(expr.name, expr)

  def transform_ForLoop(self, stmt):
    new_var = self.rename_var(stmt.var)

    merge = self.transform_merge_before_loop(stmt.merge)
    new_start = self.transform_expr(stmt.start)
    new_stop = self.transform_expr(stmt.stop)
    new_step = self.transform_expr(stmt.step)
    new_body = self.transform_block(stmt.body)
    merge = self.transform_merge_after_loop(merge)
    return ForLoop(new_var, new_start, new_stop, new_step, new_body, merge)

class LoopUnrolling(Transform):
  def __init__(self, unroll_factor = 4):
    Transform.__init__(self)
    self.unroll_factor = unroll_factor

  def transform_ForLoop(self, stmt):
    assert self.unroll_factor > 0
    if self.unroll_factor == 1:
      return stmt

    if stmt.step.__class__ is Const:
      assert stmt.step.value > 0, "Downward loops not yet supported"
    stmt = Transform.transform_ForLoop(self, stmt)
    
    if not simple_loop_body(stmt.body) or count_stmts(stmt.body) > 50:
      return stmt 
    
    const_loop_bounds = \
      stmt.start.__class__ is Const and stmt.stop.__class__ is Const

    
    cloner = CloneStmt(self.type_env)
    loop = cloner.transform_ForLoop(stmt)
    loop_var = loop.var
    loop_body = []
    loop_body.extend(loop.body)
    
    
    # if loop has static bounds, fully unroll unless it's too big
    if False and const_loop_bounds:
      iters = range(stmt.start.value, stmt.stop.value, stmt.step.value)
      if len(iters) <= 16:
        prelude = []
        for (var_name, (before_value, _)) in loop.merge:
          var = Var(var_name, type = before_value.type)
          prelude.append(Assign(var, before_value))
        
        prelude.append(Assign(loop.var, Const(iters[0], type = loop.var.type)))
        loop_body = prelude + loop_body
        
        for curr_iter in iters[1:]:
          loop_body.append(Assign(loop.var, Const(curr_iter, type = loop.var.type) ))
          prev_rename_dict = cloner.rename_dict.copy()
          loop = cloner.transform_ForLoop(stmt)
          curr_rename_dict = cloner.rename_dict
          for (var_name, (_, after_value)) in stmt.merge.iteritems():
            new_var = curr_rename_dict[var_name]
            if after_value.__class__ is Var:
              new_expr = prev_rename_dict[after_value.name]
            else:
              new_expr = after_value
            assign = Assign(new_var, new_expr)
            loop_body.append(assign)
          loop_body.extend(loop.body)
        self.blocks.top().extend(loop_body)
        return 
     
    counter_type = stmt.var.type
    unroll_value = syntax_helpers.const_int(self.unroll_factor, counter_type)

    iter_range = self.sub(stmt.stop,  stmt.start)
    big_step = self.mul(unroll_value, stmt.step)
    trunc = self.mul(self.div(iter_range, big_step), big_step)
    
    first_rename_dict = cloner.rename_dict.copy()
    
    loop_start = loop.start
    loop_stop = self.add(stmt.start, trunc, "stop")
    loop_step = big_step

    for i in xrange(1, self.unroll_factor):
      prev_rename_dict = cloner.rename_dict.copy()
      loop = cloner.transform_ForLoop(stmt)
      curr_rename_dict = cloner.rename_dict
      for (old_loop_start_name, (_, loop_end_expr)) in stmt.merge.iteritems():
        new_var = curr_rename_dict[old_loop_start_name]
        if loop_end_expr.__class__ is Var:
          new_expr = prev_rename_dict[loop_end_expr.name]
        else:
          new_expr = loop_end_expr
        assign = Assign(new_var, new_expr)
        loop_body.append(assign)
      incr = self.mul(stmt.step, syntax_helpers.const_int(i, loop.var.type))
      iter_num = self.add(loop_var, incr)
      loop_body.append(Assign(loop.var, iter_num))
      loop_body.extend(loop.body)
    final_merge  = {}
    for (loop_start_name, (left, loop_end_expr)) in stmt.merge.iteritems():
      new_loop_end_expr = cloner.transform_expr(loop_end_expr)
      loop_start_name = first_rename_dict[loop_start_name].name
      final_merge[loop_start_name] = (left, new_loop_end_expr)
    loop = ForLoop(var = loop_var,
                   start = loop_start,
                   stop = loop_stop,
                   step = loop_step,
                   body = loop_body,
                   merge = final_merge)
    self.blocks.append(loop)
    cleanup_merge = {}

    for (k,(_,r)) in stmt.merge.iteritems():
      prev_loop_value = first_rename_dict[k]
      cleanup_merge[k] = (prev_loop_value, r)
    stmt.merge = cleanup_merge
    stmt.start = loop.stop
    return stmt
