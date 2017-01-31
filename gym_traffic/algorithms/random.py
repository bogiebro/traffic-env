import tensorflow as tf
import numpy as np

flags = tf.app.flags
FLAGS = flags.FLAGS

def run(env_f):
  env = env_f()
  iterations = 0
  reward_sum = 0
  while True:
    multiplier = 1
    iterations += 1
    obs = env.reset()
    for _ in range(FLAGS.episode_len):
      obs, reward, done, _ = env.step(env.action_space.sample())
      reward_sum += np.mean(reward * multiplier)
      multiplier *= FLAGS.gamma
      if done: break
    print(reward_sum / iterations)
