import gym
from gym import error, spaces, utils
import numpy as np
import tensorflow as tf
from numba import jit, jitclass, deferred_type, void, float64, float32, int64, int32, int8
from gym.envs.classic_control import rendering
from pyglet.gl import *
import time

flags = tf.app.flags
FLAGS = flags.FLAGS
flags.DEFINE_float('global_cars_per_sec', 3, 'Cars entering the system per second')
flags.DEFINE_float('rate', 0.1, 'Number of seconds between simulator ticks')

# GL_LINES wrapper
class Lines(rendering.Geom):
  def __init__(self, vs):
    rendering.Geom.__init__(self)
    self.vs = vs
    self.linewidth = rendering.LineWidth(1)
    self.add_attr(self.linewidth)

  def render1(self):
    glBegin(GL_LINES)
    for p in self.vs: glVertex3f(p[0],p[1],0)
    glEnd()

  def set_linewidth(self, x):
    self.linewidth.stroke = x

# Get the rotation of a line segment
def get_rot(line, length):
  l = line / length
  return np.arctan2(l[1,1] - l[0,1], l[1,0] - l[0,0])

# Storage for grid graph
spec = [
    ('intersections', int64),
    ('train_roads', int64),
    ('roads', int64),
    ('entrypoints', int64[:]),
    ('locs', float32[:,:,:]),
    ('phases', int8[:]),
    ('dest', int64[:]),
    ('len', float32),
    ('n', int64),
    ('m', int64)]

# Return an array of road locations for a grid road network
@jit(float32[:,:,:](float64,int64,int64,int64,int64), nopython=True, nogil=True)
def get_locs_gridroad(eps,m,n,v,roads):
    locs = np.empty((roads,2,2), dtype=np.float32)
    for i in range(roads):
        d = i // v
        li = i % v
        col = li % n
        row = li // n
        r = i - 4*v
        if d == 0: locs[i] = np.array(((col-1,row-eps),(col,row-eps)))
        elif d == 1: locs[i] = np.array(((col+1,row+eps),(col,row+eps)))
        elif d == 2: locs[i] = np.array(((col+eps,row-1),(col+eps,row)))
        elif d == 3: locs[i] = np.array(((col-eps,row+1),(col-eps,row)))
        elif r < n: locs[i] = np.array(((r-eps,0),(r-eps,-1)))
        elif r < n+m: locs[i] = np.array(((n-1,r-n-eps),(n,r-n-eps)))
        elif r < 2*n+m: locs[i] = np.array(((r-n-m+eps,m-1),(r-n-m+eps,m)))
        else: locs[i] = np.array(((0,r-2*n-m+eps),(-1,r-2*n-m+eps)))
    return locs

# A graph representing a 2D grid with no turns
@jitclass(spec)
class GridRoad:
    def __init__(self, m, n, l):
        self.len = l
        self.n = n
        self.m = m
        v = m*n
        self.train_roads = 4*v
        self.roads = self.train_roads + 2*n + 2*m
        self.intersections = v
        self.locs = get_locs_gridroad(0.02,m,n,v,self.roads)
        self.phases = (np.arange(self.roads) // v < 2).astype(np.int8)
        self.dest = np.empty(self.roads, dtype=np.int64)
        for i in range(self.roads):
            self.dest[i] = i%v if i<4*v else -1

    # Pick a probability distribution on entrypoints
    def generate_entrypoints(self, choices):
        n = self.n
        m = self.m
        v = m * n
        emp = np.empty(0,dtype=np.int64)
        self.entrypoints = np.concatenate((
            n*np.arange(m) if (choices & 1) == 0 else emp,
            v+n*np.arange(1,m+1)-1 if ((choices >> 1) & 1) == 0 else emp,
            2*v+np.arange(n) if ((choices >> 2) & 1) == 0 else emp,
            3*v+n*(m-1)+np.arange(n) if ((choices >> 3) & 1) == 0 else emp))
                
    # Get the length of road e
    def length(self, e):
        return self.len

    # Return the road a car should go to after road i, or -1
    def next(self, i):
        v = self.intersections
        n = self.n
        m = self.m
        if i >= 4*v: return -1
        col = i % n
        row = (i % v) // n
        if i < v: return i+1 if col < n-1 else 4*v+n+row
        if i < 2*v: return i-1 if col > 0 else 4*v+2*n+m+row
        if i < 3*v: return i+n if row < m-1 else 4*v+n+m+col
        return i-n if row > 0 else 4*v+col

grid = deferred_type()
grid.define(GridRoad.class_type.instance_type)

params = 9
xi, vi, li, ai, deltai, v0i, bi, ti, s0i = range(params)
archetypes = np.zeros((1, params))
archetypes[0,vi] = 0.3
archetypes[0,ai] = 0.02
archetypes[0,deltai] = 4
archetypes[0,v0i] = 0.8
archetypes[0,li] = 0.08
archetypes[0,bi] = 0.06
archetypes[0,ti] = 1.2
archetypes[0,s0i] = 0.01

CAPACITY = 20
EPS = 1e-8

@jit(int32(int32), nopython=True, nogil=True)
def wrap(a): return 1 if a >= CAPACITY else a

@jit(void(float32,float32[:,:],float32[:,:]), nopython=True, nogil=True)
def sim(r, ld, me):
  v = me[vi]
  s_star = me[s0i] + np.maximum(0, v*me[ti] + v *
          (v - ld[vi]) / (2 * np.sqrt(me[ai]*me[bi])))
  s = ld[xi] - me[xi] - ld[li]
  dv = me[ai] * (1 - (v / me[v0i])**me[deltai] - np.square(s_star / (s + EPS)))
  dx = r*v + 0.5*dv*np.square(r)
  me[xi] += (dx > 0)*dx
  me[vi] = np.maximum(0, v + dv*r)

@jit(nopython=True, nogil=True)
def update_lights(graph, state, leading, lastcar, current_phase):
  for e in range(graph.train_roads):
    if graph.phases[e] == current_phase[graph.dest[e]]:
      state[e, xi, leading[e]] = graph.length(e)
    else:
      newrd = graph.next(e)
      if newrd >= 0 and lastcar[newrd] != leading[newrd]:
        state[e, xi, leading[e]] = state[newrd, xi, lastcar[newrd]]
        state[e, xi, leading[e]] += graph.length(e)
      else:
        state[e, xi, leading[e]] = np.inf

@jit(nopython=True, nogil=True)
def add_car(road, car, state, leading, lastcar):
  pos = wrap(lastcar[road] + 1)
  start_pos = 0
  if lastcar[road] != leading[road]:
    start_pos = state[road,xi,lastcar[road]] - state[road,li,lastcar[road]] \
        - state[road,s0i,lastcar[road]]
  if pos != leading[road]:
    state[road,:,pos] = car
    state[road,xi,pos] = min(0, start_pos)
    lastcar[road] = pos
  # else: print("Overflow")

@jit(nopython=True, nogil=True)
def advance_finished_cars(graph, state, leading, lastcar, counts):
  counts[:] = 0
  for e in range(graph.roads):
    while leading[e] != lastcar[e] and state[e,xi,wrap(leading[e]+1)] > graph.length(e):
      newlead = wrap(leading[e]+1)
      newrd = graph.next(e)
      if newrd >= 0:
        add_car(newrd, state[e,:,newlead], state, leading, lastcar)
        counts[graph.dest[e]] += 1
      state[e,:,newlead] = state[e,:,leading[e]]
      leading[e] = newlead

@jit(nopython=True, nogil=True)
def cars_on_roads(leading, lastcar):
  inverted = (leading > lastcar).astype(np.int32)
  unwrapped_lastcar = (inverted * (CAPACITY - 1)).astype(np.int32) + lastcar
  return unwrapped_lastcar - leading

def poisson(random):
  cars_per_tick = FLAGS.cars_per_sec * FLAGS.rate
  while True:
    for _ in range(int(random.exponential(1/cars_per_tick))): yield None
    yield archetypes[random.randint(archetypes.shape[0])]


class TrafficEnv(gym.Env):
  metadata = {'render.modes': ['human']}

  def _step(self, action):
    self.current_phase = np.array(action).astype(np.int8)
    update_lights(self.graph, self.state, self.leading, self.lastcar, self.current_phase)
    self.add_new_cars()
    for e in range(self.graph.roads):
      if self.leading[e] == self.lastcar[e]: continue
      if self.leading[e] < self.lastcar[e]:
        sim(FLAGS.rate, self.state[e,:,self.leading[e]:self.lastcar[e]],
            self.state[e,:,self.leading[e]+1:self.lastcar[e]+1])
      else:
        self.state[e,:,0] = self.state[e,:,-1]
        sim(FLAGS.rate, self.state[e,:,self.leading[e]:-1],
            self.state[e,:,self.leading[e]+1:])
        sim(FLAGS.rate, self.state[e,:,:self.lastcar[e]],
            self.state[e,:,1:self.lastcar[e]+1])
    advance_finished_cars(self.graph, self.state, self.leading,
        self.lastcar, self.counts)
    current_cars = cars_on_roads(self.leading, self.lastcar)[:self.graph.train_roads]
    return current_cars, self.counts, False, None

  def _reset(self):
    self.state[:,:,1] = 0 
    self.state[:,xi,1] = np.inf
    self.current_phase = np.zeros(self.graph.intersections, dtype=np.int8)
    self.rand_car = poisson(np.random.RandomState())
    self.leading = np.ones(self.graph.roads, dtype=np.int32)
    self.lastcar = np.ones(self.graph.roads, dtype=np.int32)
    return cars_on_roads(self.leading, self.lastcar)[:self.graph.train_roads]

  def add_new_cars(self):
    car = next(self.rand_car)
    while car is not None:
      add_car(np.random.choice(self.graph.entrypoints), car, self.state, self.leading, self.lastcar)
      car = next(self.rand_car)

  def init_viewer(self):
    max_x, max_y = np.max(self.graph.locs, axis=(0,1))
    min_x, min_y = np.min(self.graph.locs, axis=(0,1))
    self.viewer = rendering.Viewer(600, 600)
    self.viewer.set_bounds(min_x, max_x, min_y, max_y)
    self.roadlines = [rendering.Line(l[0],l[1]) for l in self.graph.locs]
    self.cars = [Lines([(0,0),(2,0)]) for _ in range(self.graph.roads)]
    self.roadrots = [rendering.Transform(translation=l[0], rotation=
      get_rot(l, self.graph.length(i))) for i, l in enumerate(self.graph.locs)]
    for r,c in zip(self.roadrots, self.cars): c.add_attr(r)
    for l in self.roadlines:
      l.set_color(0,1,0)
      self.viewer.add_geom(l)
    for c in self.cars:
      c.set_linewidth(5)
      c.set_color(0,0,1)
      self.viewer.add_geom(c)

  def _render(self, mode='human', close=False):
    if close:
      if self.viewer is not None:
        self.viewer.close()
        self.viewer = None
      return
    if self.viewer is None:
      self.init_viewer()
    self.update_colors()
    self.update_locs()
    time.sleep(FLAGS.rate)
    return self.viewer.render(return_rgb_array= mode=='rgb_array')

  def update_colors(self):
    for i in range(self.graph.train_roads):
      if self.graph.phases[i] == self.current_phase[self.graph.dest[i]]:
        self.roadlines[i].set_color(1,0,0)
      else:
        self.roadlines[i].set_color(0,1,0)

  def update_locs(self):
    for i in range(self.graph.roads):
      if self.leading[i] > self.lastcar[i]:
        xs,lens = np.hstack([
          self.state[i,[xi,li],self.leading[i]+1:],
          self.state[i,[xi,li],1:self.lastcar[i]+1]])
      else:
        xs,lens = self.state[i,[xi,li],self.leading[i]+1:self.lastcar[i]+1]
      if xs.shape[0] > 0:
        vals = np.concatenate(np.column_stack((xs, xs - lens)))
        self.cars[i].vs = np.column_stack((vals, np.zeros(vals.shape[0])))
      else: self.cars[i].vs = []
        
  def set_graph(self, graph):
    self.viewer = None
    self.graph = graph
    self.state = np.empty((self.graph.roads, params, CAPACITY), dtype=np.float32)
    self.action_space = spaces.MultiDiscrete([[0,1]] * graph.intersections)
    self.observation_space = spaces.Box(low=0, high=CAPACITY-2, shape=graph.train_roads)
    self.counts = np.empty(graph.intersections, dtype=np.float32)