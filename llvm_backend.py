import config
import prims
import syntax
import syntax_helpers

from core_types import BoolT, FloatT, SignedT, UnsignedT, ScalarT, NoneT
from core_types import Int32, Int64, PtrT

from llvm.core import Builder
from llvm.core import Type as lltype

import llvm_context
import llvm_convert
from llvm_helpers import const, int32 #, zero, one
import llvm_prims
import llvm_types
from llvm_types import llvm_value_type, llvm_ref_type

class Compiler(object):
  def __init__(self, fundef, llvm_cxt = llvm_context.global_context):
    self.parakeet_fundef = fundef
    self.llvm_context = llvm_cxt
    self.vars = {}
    self.initialized = set([])
    # Initializes the variables dictionary and returns a builder object
    llvm_input_types = map(llvm_ref_type, fundef.input_types)
    llvm_output_type = llvm_ref_type(fundef.return_type)
    llvm_fn_t = lltype.function(llvm_output_type, llvm_input_types)

    self.llvm_fn = self.llvm_context.module.add_function(llvm_fn_t, fundef.name)
    self.entry_block, self.entry_builder = self.new_block("entry")
    self._init_vars(self.parakeet_fundef, self.entry_builder)

  def new_block(self, name):
    bb = self.llvm_fn.append_basic_block(name)
    builder = Builder.new(bb)
    return bb, builder

  def _init_vars(self, fundef, builder):
    """
    Create a mapping from variable names to stack locations, these will later be
    converted to SSA variables by the mem2reg pass.
    """

    n_expected = len(fundef.arg_names)
    n_compiled = len(self.llvm_fn.args)
    assert n_compiled == n_expected, \
        "Expected %d args (%s) but compiled code had %d args (%s)" % \
        (n_expected, fundef.arg_names, n_compiled, self.llvm_fn.args)

    for (name, t) in fundef.type_env.iteritems():
      if not name.startswith("$"):
        llvm_t = llvm_ref_type(t)
        stack_val = builder.alloca(llvm_t, name)
        self.vars[name] = stack_val

    for llvm_arg, name in zip(self.llvm_fn.args, fundef.arg_names):
      self.initialized.add(name)
      llvm_arg.name = name
      if name in self.vars:
        builder.store(llvm_arg, self.vars[name])

  def attribute_lookup(self, struct, name, builder):
    """
    Helper for getting the address of an attribute lookup, used both when
    setting and getting attributes
    """

    llvm_struct = self.compile_expr(struct, builder)
    struct_t = struct.type
    field_pos = struct_t.field_pos(name)
    field_type = struct_t.field_type(name)
    indices = [int32(0), int32(field_pos)]
    ptr_name = "%s_ptr" % name
    ptr = builder.gep(llvm_struct, indices, ptr_name)
    return ptr, field_type

  def compile_Var(self, expr, builder):
    name = expr.name
    assert name in self.initialized, "%s uninitialized" % name
    ref = self.vars[expr.name]
    val = builder.load(ref, expr.name + "_val")
    return val

  def compile_Const(self, expr, builder):
    t = expr.type

    if isinstance(t, NoneT):
      return const(0, Int64)
    else:
      assert isinstance(expr.type, ScalarT), \
          "Expected scalar constant but got %s" % expr.type
    return const(expr.value, expr.type)

  def compile_Cast(self, expr, builder):
    llvm_value = self.compile_expr(expr.value, builder)
    return llvm_convert.convert(llvm_value, expr.value.type, expr.type, builder)

  def compile_Struct(self, expr, builder):
    struct_t = expr.type
    llvm_struct_t = llvm_value_type(struct_t)
    name = expr.type.node_type()
    struct_ptr = builder.malloc(llvm_struct_t, name + "_ptr")

    for (i, elt) in enumerate(expr.args):
      field_name, field_type = struct_t._fields_[i]
      assert elt.type == field_type, \
          "Mismatch between expected type %s and given %s for field '%s' " % \
          (field_type, elt.type, field_name)
      elt_ptr = builder.gep(struct_ptr, [int32(0), int32(i)], "field%d_ptr" % i)
      llvm_elt = self.compile_expr(elt, builder)
      builder.store(llvm_elt, elt_ptr)

    return struct_ptr

  def compile_Alloc(self, expr, builder):
    elt_t = expr.elt_type
    llvm_elt_t = llvm_types.llvm_value_type(elt_t)
    n_elts = self.compile_expr(expr.count, builder)
    return builder.malloc_array(llvm_elt_t, n_elts, "data_ptr")

  def compile_Index(self, expr, builder):
    llvm_arr = self.compile_expr(expr.value, builder)
    llvm_index = self.compile_expr(expr.index, builder)

    index_t = expr.index.type
    llvm_idx = llvm_convert.convert(llvm_index, index_t, Int32, builder)

    pointer = builder.gep(llvm_arr, [llvm_idx], "elt_pointer")
    elt = builder.load(pointer, "elt")
    return elt

  def compile_Attribute(self, expr, builder):
    field_ptr, _ = \
        self.attribute_lookup(expr.value, expr.name, builder)
    field_value = builder.load(field_ptr, "%s_value" % expr.name)
    return field_value

  def compile_TypedFn(self, expr, builder):
    (target_fn, _, _) = compile_fn(expr)
    return target_fn

  def compile_Call(self, expr, builder):
    if isinstance(expr.fn, str):
      assert expr.fn in syntax.TypedFn.registry
      typed_fundef = syntax.TypedFn.registry[expr.fn]
    else:
      assert isinstance(expr.fn, syntax.TypedFn)
      typed_fundef = expr.fn

    (target_fn, _, _) = compile_fn(typed_fundef)

    arg_types = syntax_helpers.get_types(expr.args)

    llvm_args = [self.compile_expr(arg, builder) for arg in expr.args]

    assert len(arg_types) == len(llvm_args)

    return builder.call(target_fn, llvm_args, 'call_result')

  def compile_PrimCall(self, expr, builder):
    prim = expr.prim
    args = expr.args

    # type specialization should have made types of arguments uniform,
    # so we only need to check the type of the first arg
    t = args[0].type

    llvm_args = [self.compile_expr(arg, builder) for arg in args]

    result_name = prim.name + "_result"

    if isinstance(prim, prims.Cmp):
      x, y = llvm_args
      if isinstance(t, FloatT):
        cmp_op = llvm_prims.float_comparisons[prim]
        bit = builder.fcmp(cmp_op, x, y, result_name)
      elif isinstance(t, SignedT):
        cmp_op = llvm_prims.signed_int_comparisons[prim]
        bit = builder.icmp(cmp_op, x, y, result_name)
      else:
        assert isinstance(t, UnsignedT), "Unexpected type: %s" % t
        cmp_op = llvm_prims.unsigned_int_comparisons[prim]
        bit = builder.icmp(cmp_op, x, y, result_name)
      return llvm_convert.to_bool(bit,builder)
    elif isinstance(prim, prims.Arith) or isinstance(prim, prims.Bitwise):
      if isinstance(t, FloatT):
        instr = llvm_prims.float_binops[prim]
      elif isinstance(t, SignedT):
        instr = llvm_prims.signed_binops[prim]
      elif isinstance(t, UnsignedT):
        instr = llvm_prims.unsigned_binops[prim]
      else:
        assert isinstance(t, BoolT)
        instr = llvm_prims.bool_binops[prim]
      op = getattr(builder, instr)
      return op(name = result_name, *llvm_args)
    elif isinstance(prim, prims.Logical):
      if prim == prims.logical_and:
        return builder.and_(name = result_name, *llvm_args)
      elif prim == prims.logical_not:
        return builder.not_(name = result_name, *llvm_args)
      else:
        assert prim == prims.logical_or
        return builder.or_(name = result_name, *llvm_args)
    else:
      assert False, "UNSUPPORTED PRIMITIVE: %s" % expr

  def compile_expr(self, expr, builder):
    method_name = "compile_" + expr.node_type()
    return getattr(self, method_name)(expr, builder)

  def compile_Assign(self, stmt, builder):
    rhs_t = stmt.rhs.type
    value = self.compile_expr(stmt.rhs, builder)
    if isinstance(stmt.lhs, syntax.Var):
      name = stmt.lhs.name
      lhs_t = stmt.lhs.type
      self.initialized.add(name)
      ref = self.vars[name]
    elif isinstance(stmt.lhs, syntax.Index):
      ptr_t = stmt.lhs.value.type
      assert isinstance(ptr_t, PtrT), \
          "Expected pointer, got %s" % ptr_t
      lhs_t = ptr_t.elt_type
      base_ptr = self.compile_expr(stmt.lhs.value, builder)
      index = self.compile_expr(stmt.lhs.index, builder)
      index = llvm_convert.from_signed(index, Int32, builder)
      ref = builder.gep(base_ptr, [index], "elt_ptr")
    else:
      assert isinstance(stmt.lhs, syntax.Attribute), \
          "Unexpected LHS: %s" % stmt.lhs
      struct = stmt.lhs.value
      ref, lhs_t = self.attribute_lookup(struct, stmt.lhs.name, builder)

    assert lhs_t == rhs_t, \
        "Type mismatch between LHS %s and RHS %s" % (lhs_t, rhs_t)

    builder.store(value, ref)
    return builder, False

  def compile_RunExpr(self, stmt, builder):
    self.compile_expr(stmt.value, builder)
    return builder, False

  def compile_Return(self, stmt, builder):
    ret_val = self.compile_expr(stmt.value, builder)
    builder.ret(ret_val)
    return builder, True

  def compile_merge_left(self, phi_nodes, builder):
    for name, (left, _) in phi_nodes.iteritems():
      ref = self.vars[name]
      self.initialized.add(name)
      value = self.compile_expr(left, builder)
      builder.store(value, ref)

  def compile_merge_right(self, phi_nodes, builder):
    for name, (_, right) in phi_nodes.iteritems():
      ref = self.vars[name]
      self.initialized.add(name)
      value = self.compile_expr(right, builder)
      builder.store(value, ref)

  def compile_While(self, stmt, builder):
    # current flow ----> loop --------> exit--> after
    #    |                       skip------------|
    #    |----------------------/

    self.compile_merge_left(stmt.merge, builder)
    loop_bb, body_start_builder = self.new_block("loop_body")

    after_bb, after_builder = self.new_block("after_loop")
    enter_cond = self.compile_expr(stmt.cond, builder)
    enter_cond = llvm_convert.to_bit(enter_cond, builder)
    builder.cbranch(enter_cond, loop_bb, after_bb)

    body_end_builder, body_always_returns = \
        self.compile_block(stmt.body, body_start_builder)
    if not body_always_returns:
      exit_bb, exit_builder = self.new_block("loop_exit")
      self.compile_merge_right(stmt.merge, body_end_builder)
      repeat_cond = self.compile_expr(stmt.cond, body_end_builder)
      repeat_cond = llvm_convert.to_bit(repeat_cond, body_end_builder)
      body_end_builder.cbranch(repeat_cond, loop_bb, exit_bb)
      exit_builder.branch(after_bb)

    return after_builder, False

  def compile_If(self, stmt, builder):
    cond = self.compile_expr(stmt.cond, builder)
    cond = llvm_convert.to_bit(cond, builder)

    # compile the two possible branches as distinct basic blocks
    # and then wire together the control flow with branches
    true_bb, true_builder = self.new_block("if_true")
    after_true, true_always_returns = \
        self.compile_block(stmt.true, true_builder)

    false_bb, false_builder = self.new_block("if_false")
    after_false, false_always_returns = \
        self.compile_block(stmt.false, false_builder)

    builder.cbranch(cond, true_bb, false_bb)

    # compile phi nodes as assignments and then branch
    # to the continuation block
    self.compile_merge_left(stmt.merge, after_true)
    self.compile_merge_right(stmt.merge, after_false)

    # if both branches return then there is no point
    # making a new block for more code
    # did both branches end in a return?
    both_always_return = true_always_returns and false_always_returns
    if both_always_return:
      return None, True
    else:
      after_bb, after_builder = self.new_block("if_after")
      if not true_always_returns:
        after_true.branch(after_bb)
      if not false_always_returns:
        after_false.branch(after_bb)
    return after_builder, False

  def compile_stmt(self, stmt, builder):
    """
    Translate an SSA statement into LLVM. Every translation function returns a
    builder pointing to the end of the current basic block and a boolean
    indicating whether every branch of control flow in that statement ends in a
    return. The latter is needed to avoid creating empty basic blocks, which were
    causing some mysterious crashes inside LLVM.
    """

    method_name = "compile_" + stmt.node_type()
    return getattr(self, method_name)(stmt, builder)

  def compile_block(self, stmts, builder):

    for stmt in stmts:
      builder, always_returns = self.compile_stmt(stmt, builder)
      if always_returns:
        return builder, always_returns
    return builder, False

  def compile_body(self, body):
    return self.compile_block(body, builder = self.entry_builder)

compiled_functions = {}
def compile_fn(fundef):

  if fundef.name in compiled_functions:
    return compiled_functions[fundef.name]
  compiler = Compiler(fundef)
  compiler.compile_body(fundef.body)

  if config.print_unoptimized_llvm:
    print "=== LLVM before optimizations =="
    print
    print compiler.llvm_fn
    print
  compiler.llvm_context.run_passes(compiler.llvm_fn)

  if config.print_optimized_llvm:
    print "=== LLVM after optimizations =="
    print
    print compiler.llvm_fn
    print

  result = (compiler.llvm_fn, fundef, compiler.llvm_context.exec_engine)
  compiled_functions[fundef.name] = result
  return result
