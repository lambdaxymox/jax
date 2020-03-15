from functools import partial
from collections import Counter

import numpy as onp
from scipy.special import factorial as fact

from jax import core
from jax.util import unzip2, prod
from jax.tree_util import (register_pytree_node, tree_structure,
                           treedef_is_leaf, tree_flatten, tree_unflatten)
import jax.linear_util as lu


def jet(fun, primals, series):
  try:
    order, = set(map(len, series))
  except ValueError:
    msg = "jet terms have inconsistent lengths for different arguments"
    raise ValueError(msg) from None

  # TODO(mattjj): consider supporting pytree inputs
  for i, (x, terms) in enumerate(zip(primals, series)):
    treedef = tree_structure(x)
    if not treedef_is_leaf(treedef):
      raise ValueError("primal value at position {} is not an array".format(i))
    for j, t in enumerate(terms):
      treedef = tree_structure(t)
      if not treedef_is_leaf(treedef):
        raise ValueError("term {} for argument {} is not an array".format(j, i))

  @lu.transformation_with_aux
  def flatten_fun_output(*args):
    ans = yield args, {}
    yield tree_flatten(ans)

  f, out_tree = flatten_fun_output(lu.wrap_init(fun))
  out_primals, out_terms = jet_transform(f).call_wrapped(primals, series)
  return tree_unflatten(out_tree(), out_primals), tree_unflatten(out_tree(), out_terms)

@lu.transformation
def jet_transform(primals, series):
  with core.new_master(JetTrace) as master:
    trace = JetTrace(master, core.cur_sublevel())
    in_tracers = map(partial(JetTracer, trace), primals, series)
    ans = yield in_tracers, {}
    out_tracers = map(trace.full_raise, ans)
    out_primals, out_terms = unzip2((t.primal, t.terms) for t in out_tracers)
  yield out_primals, out_terms


class JetTracer(core.Tracer):
  __slots__ = ["primal", "terms"]

  def __init__(self, trace, primal, terms):
    assert type(terms) in (ZeroSeries, list, tuple)
    self._trace = trace
    self.primal = primal
    self.terms = terms

  @property
  def aval(self):
    return core.get_aval(self.primal)

  def full_lower(self):
    if self.terms is zero_series or all(t is zero_term for t in self.terms):
      return core.full_lower(self.primal)
    else:
      return self

class JetTrace(core.Trace):

  def pure(self, val):
    return JetTracer(self, val, zero_series)

  def lift(self, val):
    return JetTracer(self, val, zero_series)

  def sublift(self, val):
    return JetTracer(self, val.primal, val.terms)

  def process_primitive(self, primitive, tracers, params):
    primals_in, series_in = unzip2((t.primal, t.terms) for t in tracers)
    order, = {len(terms) for terms in series_in if terms is not zero_series}
    series_in = [[zero_term] * order if s is zero_series else s
                 for s in series_in]
    # TODO(mattjj): avoid always instantiating zeros
    series_in = [[onp.zeros(onp.shape(x), dtype=onp.result_type(x))
                  if t is zero_term else t for t in series]
                 for x, series in zip(primals_in, series_in)]
    rule = jet_rules[primitive]
    primal_out, terms_out = rule(primals_in, series_in, **params)
    return JetTracer(self, primal_out, terms_out)

  def process_call(self, call_primitive, f, tracers, params):
    assert False  # TODO

  def post_process_call(self, call_primitive, out_tracer, params):
    assert False  # TODO

  def join(self, xt, yt):
    assert False  # TODO?


class ZeroTerm(object): pass
zero_term = ZeroTerm()
register_pytree_node(ZeroTerm, lambda z: ((), None), lambda _, xs: zero_term)

class ZeroSeries(object): pass
zero_series = ZeroSeries()
register_pytree_node(ZeroSeries, lambda z: ((), None), lambda _, xs: zero_series)


jet_rules = {}

def deflinear(prim):
  jet_rules[prim] = partial(linear_prop, prim)

def linear_prop(prim, primals_in, series_in, **params):
  primal_out = prim.bind(*primals_in, **params)
  series_out = [prim.bind(*terms_in, **params) for terms_in in zip(*series_in)]
  return primal_out, series_out


### rule definitions

from jax.lax import lax

deflinear(lax.neg_p)
deflinear(lax.real_p)
deflinear(lax.complex_p)
deflinear(lax.add_p)
deflinear(lax.sub_p)
deflinear(lax.convert_element_type_p)
deflinear(lax.broadcast_p)
deflinear(lax.broadcast_in_dim_p)
deflinear(lax.concatenate_p)
deflinear(lax.pad_p)
deflinear(lax.reshape_p)
deflinear(lax.rev_p)
deflinear(lax.transpose_p)
deflinear(lax.slice_p)
deflinear(lax.reduce_sum_p)
deflinear(lax.reduce_window_sum_p)
deflinear(lax.tie_in_p)

def _exp_taylor(primals_in, series_in):
  x, = primals_in
  series, = series_in
  u = [x] + series
  v = [lax.exp(x)] + [None] * len(series)
  def scale(k, j): return 1. / (fact(k-j) * fact(j-1))
  for k in range(1,len(v)):
    v[k] = fact(k-1) * sum([scale(k, j)* v[k-j] * u[j] for j in range(1, k+1)])
  primal_out, *series_out = v
  return primal_out, series_out
jet_rules[lax.exp_p] = _exp_taylor

def _log_taylor(primals_in, series_in):
  x, = primals_in
  series, = series_in
  u = [x] + series
  v = [lax.log(x)] + [None] * len(series)
  def scale(k, j): return 1. / (fact(k-j) * fact(j-1))
  for k in range(1, len(v)):
    conv = sum([scale(k, j) * v[j] * u[k-j] for j in range(1, k)])
    v[k] = (u[k] - fact(k - 1) * conv) / u[0]
  primal_out, *series_out = v
  return primal_out, series_out
jet_rules[lax.log_p] = _log_taylor

def _div_taylor_rule(primals_in, series_in, **params):
  x, y = primals_in
  x_terms, y_terms = series_in
  u = [x] + x_terms
  w = [y] + y_terms
  v = [None] * len(u)
  def scale(k, j): return 1. / (fact(k - j) * fact(j))
  for k in range(0, len(v)):
    conv = sum([scale(k, j) * v[j] * w[k-j] for j in range(0, k)])
    v[k] = (u[k] - fact(k) * conv) / w[0]
  primal_out, *series_out = v
  return primal_out, series_out
jet_rules[lax.div_p] = _div_taylor_rule

def _bilinear_taylor_rule(prim, primals_in, series_in, **params):
  x, y = primals_in
  x_terms, y_terms = series_in
  u = [x] + x_terms
  w = [y] + y_terms
  v = [None] * len(u)
  op = partial(prim.bind, **params)
  def scale(k, j): return 1. / (fact(k - j) * fact(j))
  for k in range(0, len(v)):
    v[k] = fact(k) * sum([scale(k, j) * op(u[j], w[k-j]) for j in range(0, k+1)])
  primal_out, *series_out = v
  return primal_out, series_out
jet_rules[lax.dot_general_p] = partial(_bilinear_taylor_rule, lax.dot_general_p)
jet_rules[lax.mul_p] = partial(_bilinear_taylor_rule, lax.mul_p)
jet_rules[lax.conv_general_dilated_p] = partial(_bilinear_taylor_rule, lax.conv_general_dilated_p)

def _gather_taylor_rule(primals_in, series_in, **params):
  operand, start_indices = primals_in
  gs, _ = series_in
  primal_out = lax.gather_p.bind(operand, start_indices, **params)
  series_out = [lax.gather_p.bind(g, start_indices, **params) for g in gs]
  return primal_out, series_out
jet_rules[lax.gather_p] = _gather_taylor_rule

def _reduce_max_taylor_rule(primals_in, series_in, **params):
  operand, = primals_in
  gs, = series_in
  primal_out = lax.reduce_max_p.bind(operand, **params)
  axes = params.pop("axes", None)
  primal_dtype = gs[0].dtype
  shape = [1 if i in axes else d for i, d in enumerate(operand.shape)]
  location_indicators = lax.convert_element_type(
        lax._eq_meet(operand, lax.reshape(primal_out, shape)), primal_dtype)
  counts = lax._reduce_sum(location_indicators, axes)
  def _reduce_chooser_taylor_rule(g):
    return lax.div(lax._reduce_sum(lax.mul(g, location_indicators), axes), counts)
  series_out = [_reduce_chooser_taylor_rule(g) for g in gs]
  return primal_out, series_out
jet_rules[lax.reduce_max_p] = _reduce_max_taylor_rule


from jax.interpreters import xla
deflinear(xla.device_put_p)
