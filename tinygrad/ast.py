from enum import Enum
import itertools
from typing import List, Tuple
from tinygrad.helpers import prod, dedup, all_same
from tinygrad.ops import LazyOp, MovementOps, get_lazyop_info, get_buffers, ReduceOps, get_lazyops
from tinygrad.shape import ShapeTracker, View

def get_first_reduce(shapes):
  for i in range(len(shapes[0])):
    if not all_same([x[i] for x in shapes]):
      return i
  return len(shapes[0])  # off the end

Types = Enum("Types", ["FLOAT", "FLOAT4"])
class Token:
  def __init__(self, tok:str, typ:Types, ptr:bool=False):
    assert isinstance(tok, str)
    self.tok, self.typ, self.ptr = tok, typ, ptr
    self.axis : List[Tuple[int, int]] = []
  def array(self, length, stride): self.axis.append((length, stride))
  def size(self): return prod(x[0] for x in self.axis)
  def offsets(self): return [sum(t) for t in itertools.product(*[[y*x[1] for y in range(x[0])] for x in self.axis[::-1]])] if len(self.axis) else [0]
  def decltype(self): return ('float' if self.typ == Types.FLOAT else 'float4') + ('*' if self.ptr else '')
  def __repr__(self): return f"<{self.typ}{'*' if self.ptr else ''} {self.tok}{f'[{self.axis}]' if len(self.axis) else ''}>"

# ast kernel can contain one ReduceOp with arbitrary Binary/Unary ops
class ASTKernel:
  def __init__(self, ast:LazyOp):
    # key for lookup in cache (can change, str might not be right)
    self.input_ast = ast
    self.key = str(ast)

    # if the AST ends with a RESHAPE, we remove it and create the buffer accordingly
    if ast.op == MovementOps.RESHAPE:
      output_shape = ast.arg
      ast = ast.src[0]
    else:
      output_shape = None

    self.info = get_lazyop_info(ast)
    self.bufs = dedup(get_buffers(ast))
    self.ast = ast

    # create the buffer we are returning (as the same type as the input buffers) and add it as the first buffer
    self.ret = type(self.bufs[0])(output_shape if output_shape else self.info.shape, force_create=True)
    self.bufs = [type(self.ret)(self.info.shape, hostbuf=self.ret)] + self.bufs

    # TODO: should be optional if it's hitting a function cache
    self.process()

  def process(self):
    reduceops = [x for x in get_lazyops(self.ast) if x.op in ReduceOps]
    assert len(dedup(reduceops)) <= 1, "max one reduce op in an ast"
    self.reduceop = reduceops[0] if reduceops else None
    self.earlybufs = dedup(get_buffers(self.reduceop)) if self.reduceop else []

    self.buftokens = [Token(f"data{i}", Types.FLOAT, ptr=True) for i in range(len(self.bufs))]
    self.group_for_reduce : List[int] = []

    # check valid AST kernel
    assert all_same([x.shape for x in self.earlybufs]), "all earlybufs must have the same shape"
    assert all_same([x.shape for x in self.bufs if x not in self.earlybufs]), "all latebufs must have the same shape"
    assert all_same([len(x.shape) for x in self.bufs]), "all bufs must have the same shape size"

    # process
    self.sts : List[ShapeTracker] = [x.st.copy() for x in self.bufs]   # create new shapetrackers inside this kernel
    self.simplify_ones()
    self.simplify_merge_adjacent()

  def print(self):
    buf_count = -1
    op_count = -1
    cache = {}
    def print_ast(x, name=None):
      nonlocal buf_count, op_count
      if x not in cache:
        if not isinstance(x, LazyOp):
          if name is None:
            buf_count += 1
            name = f"buf{buf_count}"
          print(f"buf{buf_count} = {x}")
          cache[x] = name
        else:
          srcs = [print_ast(y) for y in x.src]
          if name is None:
            op_count += 1
            name = f"op{op_count}"
          print(f"{name} = LazyOp({str(x.op)}, ({','.join(srcs)},), {x.arg})")
          cache[x] = name
      return cache[x]
    print_ast(self.input_ast, "ast")

  @property
  def shape_len(self): return len(self.sts[0].shape)

  def simplify_ones(self):
    # remove places where the shape is all ones
    # TODO: this should be factored in to multi shape stride
    all_ones = [all(st.shape[i]==1 for st in self.sts) for i in range(self.shape_len)]
    # keep at least 1 one
    if all(all_ones): all_ones[-1] = False
    self.reshape_and_permute(lambda shape: [x for i,x in enumerate(shape) if not all_ones[i]], None)
    # find first mismatch, don't reduce this
    self.first_reduce = get_first_reduce([x.shape for x in self.sts])

  def simplify_merge_adjacent(self):
    shapes, strides = [x.shape for x in self.sts], [x.views[-1].strides for x in self.sts]

    # merge dimensions if we can, multi get_shape_strides
    # TODO: does this always preserve the reduce dimension, NO
    # TODO: move this into shapetracker, with tests!
    rets = [[(shapes[j][0], strides[j][0])] for j in range(len(shapes))]
    for i in range(1, len(shapes[0])):
      can_merge = []
      for j in range(len(shapes)):
        # TODO: added the always mergeability of 1s, is this right? if so, add to shapetracker in the 1 case
        can_merge.append((strides[j][i] != 0 and rets[j][-1][1] == shapes[j][i]*strides[j][i]) or (strides[j][i] == 0 and rets[j][-1][1] == 0))
      # more can merge than this
      mergeable = all(can_merge) and i != self.first_reduce
      for j in range(len(shapes)):
        if mergeable:
          rets[j][-1] = (rets[j][-1][0] * shapes[j][i], strides[j][i])
        else:
          rets[j].append((shapes[j][i], strides[j][i]))

    for i,x in enumerate(rets): self.sts[i].reshape(*[y[0] for y in x])
    self.first_reduce = get_first_reduce([x.shape for x in self.sts])

  # this should be aware of the three parts to the shape
  #  * the input/output dimensions
  #  * the reduce dimensions
  #  * the size outputted by each kernel
  def reshape_and_permute(self, new_shape_fxn, axis):
    for st in self.sts:
      if new_shape_fxn is not None: st.reshape(*new_shape_fxn(st.shape))
      if axis is not None: st.permute(*axis)

  # drops the final dimension
  def upcast(self):
    upcasted = [x.shape[-1] for x in self.sts if x.shape[-1] != 1]
    assert len(upcasted) >= 1 and all_same(upcasted), f"can't upcast mismatch {upcasted}"
    for i in range(len(self.bufs)):
      st = self.sts[i]
      if st.shape[-1] == upcasted[0]:
        # multiview shapetrackers can slice through a float4, so don't allow them
        can_merge = (not st.needs_valid() and len(st.views) == 1) or "Image" in str(type(self.bufs[i]._buf))  # TODO: terrible hack
        if st.shape[-1] == 4 and self.buftokens[i].typ == Types.FLOAT and st.views[-1].strides[-1] == 1 and can_merge:
          # this is an upcast to FLOAT4
          self.buftokens[i].typ = Types.FLOAT4
          assert all(st.views[-1].strides[i]%upcasted[0] == 0 or st.views[-1].shape[i] == 1 for i in range(len(st.shape)-1))
          assert self.sts[i].offset % upcasted[0] == 0
        else:
          self.buftokens[i].array(upcasted[0], st.views[-1].strides[-1])

    # remove the last dimension
    for st in self.sts: st.views[-1] = View(st.shape[0:-1], st.views[-1].strides[0:-1], st.views[-1].offset)