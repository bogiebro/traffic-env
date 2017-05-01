from itertools import count
import math
import numpy as np
from args import FLAGS

def square(x): return x*x

def forever(f):
  while True: yield f()

def print_running_stats(iterator):
  reward_mean = 0
  reward_var = 0
  for iterations in count(1):
    reward = next(iterator)
    reward_mean = (reward + (iterations - 1) * reward_mean) / iterations
    if iterations >= 2:
      reward_var = (iterations - 2) / (iterations - 1) * reward_var + \
          square(reward - reward_mean) / iterations
    print("Reward %2f\t Mean %2f\t Std %2f" % (reward, reward_mean, math.sqrt(reward_var)))

def episode_reward(gen):
  reward = 0.0
  multiplier = 1.0
  for (i,_,_,r,*_) in gen:
    reward += np.mean(r) * (multiplier if FLAGS.print_discounted else 1)
    multiplier *= FLAGS.gamma
  if not FLAGS.use_avg:
    denom = 1
  elif FLAGS.gamma == 1:
    denom = i+1
  else:
    denom = (math.pow(FLAGS.gamma, i+1) - 1) / (FLAGS.gamma - 1) 
  return reward / denom
