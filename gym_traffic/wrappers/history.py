import gym
import numpy as np
from collections import deque

def HistoryWrapper(history_count):
  class HistoryWrapper(gym.Wrapper):
    def __init__(self, env):
      super(HistoryWrapper, self).__init__(env)
      self.history_count = history_count
      self.history = deque()
      self.observation_space = env.observation_space.replicated(history_count)

    def _step(self, action):
      obs, reward, done, info = self.env.step(action)
      self.history.popleft()
      self.history.append(obs)
      return np.array(self.history), reward, done, info

    def _reset(self):
      self.history.clear()
      self.history.append(self.env.reset())
      for _ in range(self.history_count - 1):
        self.history.append(self.env.step(self.env.action_space.sample())[0])
      return np.stack(self.history)
    
  return HistoryWrapper
