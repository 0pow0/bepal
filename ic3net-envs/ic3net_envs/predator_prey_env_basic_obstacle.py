#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Simulate a predator prey environment.
Each agent can just observe itself (it's own identity) i.e. s_j = j and vision sqaure around it.

Design Decisions:
    - Memory cheaper than time (compute)
    - Using Vocab for class of box:
         -1 out of bound,
         indexing for predator agent (from 2?)
         ??? for prey agent (1 for fixed case, for now)
    - Action Space & Observation Space are according to an agent
    - Rewards -0.05 at each time step till the time
    - Episode never ends
    - Obs. State: Vocab of 1-hot < predator, preys & units >
"""

# core modules
import copy

import random
import math
import curses

# 3rd party modules
import gym
import numpy as np
from gym import spaces
import random

class PredatorPreyEnv(gym.Env):
    # metadata = {'render.modes': ['human']}

    def __init__(self,):
        self.__version__ = "0.0.1"

        # TODO: better config handling
        self.OUTSIDE_CLASS = 1
        self.PREY_CLASS = 2
        self.PREDATOR_CLASS = 3
        self.GRID_CLASS = 4
        self.TIMESTEP_PENALTY = -0.05
        self.PREY_REWARD = 0
        self.POS_PREY_REWARD = 0.05
        self.episode_over = False
        self.map_dim = 4

    def init_curses(self):
        self.stdscr = curses.initscr()
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_RED, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_CYAN, -1)
        curses.init_pair(4, curses.COLOR_GREEN, -1)


    def init_args(self, parser):
        env = parser.add_argument_group('Prey Predator task')
        env.add_argument('--nenemies', type=int, default=1,
                         help="Total number of preys in play")
        env.add_argument('--dim', type=int, default=5,
                         help="Dimension of box")
        env.add_argument('--vision', type=int, default=2,
                         help="Vision of predator")
        env.add_argument('--moving_prey', action="store_true", default=False,
                         help="Whether prey is fixed or moving")
        env.add_argument('--no_stay', action="store_true", default=False,
                         help="Whether predators have an action to stay in place")
        parser.add_argument('--mode', default='mixed', type=str,
                        help='cooperative|competitive|mixed (default: mixed)')
        env.add_argument('--enemy_comm', action="store_true", default=False,
                         help="Whether prey can communicate.")

    def multi_agent_init(self, args):

        # General variables defining the environment : CONFIG
        params = ['dim', 'vision', 'moving_prey', 'mode', 'enemy_comm']
        for key in params:
            setattr(self, key, getattr(args, key))

        self.nprey = args.nenemies
        self.nprey = 1
        self.npredator = args.nfriendly
        self.dims = dims = (self.dim, self.dim)
        self.stay = not args.no_stay
        self.ngrid = args.obstacles

        if args.moving_prey:
            raise NotImplementedError
            # TODO

        # (0: UP, 1: RIGHT, 2: DOWN, 3: LEFT, 4: STAY)
        # Define what an agent can do -
        if self.stay:
            self.naction = 5
        else:
            self.naction = 4

        self.action_space = spaces.MultiDiscrete([self.naction])

        self.BASE = (dims[0] * dims[1])
        self.OUTSIDE_CLASS += self.BASE
        self.PREY_CLASS += self.BASE
        self.PREDATOR_CLASS += self.BASE
        self.GRID_CLASS += self.BASE
        # embed n*n*3
        self.true = np.zeros([self.map_dim, dims[0], dims[1]])
        self.padding = np.zeros([self.map_dim, dims[0]+2*self.vision, dims[1]+2*self.vision])
        self.agent_udt = np.zeros([self.npredator, 4, dims[0], dims[1]])
        self.ppweight = 2
        self.min_steps = 0
        self.comm = np.zeros([self.npredator])
        self.observed_obstacle = np.zeros(self.ngrid)
        # Setting max vocab size for 1-hot encoding
        self.vocab_size = 1 + 1 + self.BASE + 1 + 1
        #          predator + prey + grid + outside

        # Observation for each agent will be vision * vision ndarray
        #self.observation_space = spaces.Box(low=0, high=1, shape=(self.vocab_size, (2 * self.vision) + 1, (2 * self.vision) + 1), dtype=int)
        self.observation_space = spaces.Box(low=0, high=1, shape=(4, self.dim, self.dim), dtype=int) # change input to m*m*3
        # Actual observation will be of the shape 1 * npredator * (2v+1) * (2v+1) * vocab_size

        return

    def step(self, action):
        """
        The agents take a step in the environment.

        Parameters
        ----------
        action : list/ndarray of length m, containing the indexes of what lever each 'm' chosen agents pulled.

        Returns
        -------
        obs, reward, episode_over, info : tuple
            obs (object) :

            reward (float) : Ratio of Number of discrete levers pulled to total number of levers.
            episode_over (bool) : Will be true as episode length is 1
            info (dict) : diagnostic information useful for debugging.
        """
        self.comm = action[1]
        action = action[0]
        if self.episode_over:
            raise RuntimeError("Episode is done")
        action = np.array(action).squeeze()
        action = np.atleast_1d(action)

        for i, a in enumerate(action):
            self._take_action(i, a)

        assert np.all(action <= self.naction), "Actions should be in the range [0,naction)."


        self.episode_over = False
        self.obs = self._get_obs()

        debug = {'predator_locs':self.predator_loc,'prey_locs':self.prey_loc}
        return self.obs, self._get_reward(), self.episode_over, debug

    def reset(self):
        """
        Reset the state of the environment and returns an initial observation.

        Returns
        -------
        observation (object): the initial observation of the space.
        """
        self.episode_over = False
        self.reached_prey = np.zeros(self.npredator)

        # Locations
        locs = self._get_cordinates() #original without obstacle
        # self.predator_loc, self.prey_loc = locs[:self.npredator], locs[self.npredator:]
        self.predator_loc, self.prey_loc, self.grid_loc = locs[:self.npredator], locs[self.npredator:
                                                                                      self.npredator + self.nprey], \
                                                          locs[self.npredator + self.nprey:]

        for cord in locs[:self.npredator + self.nprey].tolist():
            if cord[0] == 0:
                if cord[1] == 0:
                    sur = [[cord[0] + 1, cord[1]], [cord[0], cord[1] + 1]]
                elif cord[1] == self.dim - 1:
                    sur = [[cord[0] + 1, cord[1]], [cord[0], cord[1] - 1]]
                else:
                    sur = [[cord[0] + 1, cord[1]], [cord[0], cord[1] + 1], [cord[0], cord[1] - 1]]
            elif cord[0] == self.dim - 1:
                if cord[1] == 0:
                    sur = [[cord[0] - 1, cord[1]], [cord[0], cord[1] + 1]]
                elif cord[1] == self.dim - 1:
                    sur = [[cord[0] - 1, cord[1]], [cord[0], cord[1] - 1]]
                else:
                    sur = [[cord[0] - 1, cord[1]], [cord[0], cord[1] + 1], [cord[0], cord[1] - 1]]
            elif cord[1] == 0:
                sur = [[cord[0] + 1, cord[1]], [cord[0] - 1, cord[1]], [cord[0], cord[1] + 1]]
            elif cord[1] == self.dim - 1:
                sur = [[cord[0] + 1, cord[1]], [cord[0] - 1, cord[1]], [cord[0], cord[1] - 1]]
            else:
                sur = [[cord[0] + 1, cord[1]], [cord[0] - 1, cord[1]], [cord[0], cord[1] + 1], [cord[0], cord[1] - 1]]
            d = [False for c in sur if c not in self.grid_loc.tolist()]
            if not d:
                self.reset()

        # self.get_min_steps()

        self._set_grid()

        # stat - like success ratio
        self.stat = dict()

        # Observation will be npredator * vision * vision ndarray
        self.obs = self._get_obs()
        return self.obs

    def seed(self):
        return

    def get_min_steps(self):
        min_s = 0
        for i,j in self.predator_loc:
            if self.prey_loc.shape[0] == 1:
                temp = abs(i-self.prey_loc[0][0]) + abs(j-self.prey_loc[0][1])
                if temp > min_s:
                    min_s = temp
            else:
                return 999
        # self.min_steps = min_s pretrain

    def embed_grid(self):
        self.true = np.zeros([self.map_dim, self.dims[0], self.dims[1]])
        for i, p in enumerate(self.predator_loc):
            self.true[0, p[0], p[1]] = self.ppweight
        for i, p in enumerate(self.prey_loc):
            self.true[1, p[0], p[1]] = 2
        for i, p in enumerate(self.grid_loc):
            self.true[3, p[0], p[1]] = 2
        slice_y = slice(self.vision, self.padding.shape[1] - self.vision)
        slice_x = slice(self.vision, self.padding.shape[1] - self.vision)
        self.padding[1, slice_y, slice_x] = self.true[1, :, :]
        self.padding[0, slice_y, slice_x] = self.true[0, :, :]
        self.padding[3, slice_y, slice_x] = self.true[3, :, :]



    def _get_cordinates(self):
        idx = np.random.choice(np.prod(self.dims),(self.npredator + self.nprey+ self.ngrid), replace=False)
        return np.vstack(np.unravel_index(idx, self.dims)).T

    def _set_grid(self):
        self.grid = np.arange(self.BASE).reshape(self.dims)
        # Mark agents in grid
        # self.grid[self.predator_loc[:,0], self.predator_loc[:,1]] = self.predator_ids
        # self.grid[self.prey_loc[:,0], self.prey_loc[:,1]] = self.prey_ids

        # Padding for vision
        self.grid = np.pad(self.grid, self.vision, 'constant', constant_values = self.OUTSIDE_CLASS)

        self.empty_bool_base_grid = self._onehot_initialization(self.grid)

    def _get_obs(self):
        self.bool_base_grid = self.empty_bool_base_grid.copy()
        self.embed_grid()
        padding_t = np.zeros([self.dim + self.vision*2, self.dim + self.vision*2])

        for i, p in enumerate(self.predator_loc):
            self.bool_base_grid[p[0] + self.vision, p[1] + self.vision, self.PREDATOR_CLASS] += 1

        for i, p in enumerate(self.prey_loc):
            self.bool_base_grid[p[0] + self.vision, p[1] + self.vision, self.PREY_CLASS] += 1

        obs = []
        myobs = []
        for i, p in enumerate(self.predator_loc):
            ept = np.zeros([self.map_dim, self.dim + self.vision * 2, self.dim + self.vision * 2])
            slice_y = slice(p[0], p[0] + (2 * self.vision) + 1)
            slice_x = slice(p[1], p[1] + (2 * self.vision) + 1)
            obs.append(self.bool_base_grid[slice_y, slice_x])
            padding_t[slice_y, slice_x] = 1
            self.padding[2, slice_y, slice_x] = padding_t[slice_y, slice_x]
            ept[:, slice_y, slice_x] = self.padding[:, slice_y, slice_x]
            my_y = slice(self.vision, padding_t.shape[0] - self.vision)
            my_x = slice(self.vision, padding_t.shape[0] - self.vision)
            self.agent_udt[i, :, :, :] = copy.deepcopy(ept[:, my_y, my_x])
            myobs.append(ept[:, my_y, my_x])

        if self.enemy_comm:
            for p in self.prey_loc:
                slice_y = slice(p[0], p[0] + (2 * self.vision) + 1)
                slice_x = slice(p[1], p[1] + (2 * self.vision) + 1)
                obs.append(self.bool_base_grid[slice_y, slice_x])
        slice_y = slice(self.vision, padding_t.shape[0]-self.vision)
        slice_x = slice(self.vision, padding_t.shape[0]-self.vision)

        self.true[2, :, :] = padding_t[slice_y, slice_x]
        myobs = np.stack(myobs)
        grid = np.where(myobs[:,3,:,:]>0)
        for i in range(len(grid[0])):
            g_x = np.where(self.grid_loc[:,0]==grid[1][i] ) # self.grid_loc[:,1]==test[2][i]).any()
            g_y = np.where(self.grid_loc[:, 1] == grid[2][i])
            answer = np.intersect1d(g_x[0], g_y[0], True)
            self.observed_obstacle[answer[0]] = 1
        return myobs

    def _take_action(self, idx, act):
        # prey action
        if idx >= self.npredator:
            # fixed prey
            if not self.moving_prey:
                return
            else:
                raise NotImplementedError

        if self.reached_prey[idx] == 1:
            return

        # STAY action
        if act==5:
            return

        location = copy.deepcopy(self.predator_loc[idx])
        if act == 0:
            location[0] = location[0] - 1
        elif act == 1:
            location[1] = location[1] + 1
        elif act == 2:
            location[0] = location[0] + 1
        elif act == 3:
            location[1] = location[1] - 1

        # UP
        if act == 0 and self.grid[max(0,
                                      self.predator_loc[idx][0] + self.vision - 1),
                                  self.predator_loc[idx][1] + self.vision] != self.OUTSIDE_CLASS \
            and location.tolist() not in self.grid_loc.tolist():
            self.predator_loc[idx][0] = max(0, self.predator_loc[idx][0] - 1)

        # RIGHT
        elif act == 1 and self.grid[self.predator_loc[idx][0] + self.vision,
                                    min(self.dims[1] - 1,
                                        self.predator_loc[idx][1] + self.vision + 1)] != self.OUTSIDE_CLASS \
            and location.tolist() not in self.grid_loc.tolist():
            self.predator_loc[idx][1] = min(self.dims[1] - 1,
                                            self.predator_loc[idx][1] + 1)

        # DOWN
        elif act == 2 and self.grid[min(self.dims[0] - 1,
                                        self.predator_loc[idx][0] + self.vision + 1),
                                    self.predator_loc[idx][1] + self.vision] != self.OUTSIDE_CLASS \
            and location.tolist() not in self.grid_loc.tolist():
            self.predator_loc[idx][0] = min(self.dims[0] - 1,
                                            self.predator_loc[idx][0] + 1)

        # LEFT
        elif act == 3 and self.grid[self.predator_loc[idx][0] + self.vision,
                                    max(0,
                                        self.predator_loc[idx][1] + self.vision - 1)] != self.OUTSIDE_CLASS \
            and location.tolist() not in self.grid_loc.tolist():
            self.predator_loc[idx][1] = max(0, self.predator_loc[idx][1] - 1)

    def _get_reward(self):
        n = self.npredator if not self.enemy_comm else self.npredator + self.nprey
        reward = np.full(n, self.TIMESTEP_PENALTY)

        # on_prey = np.where(np.all(self.predator_loc == self.prey_loc[0], axis=1))[0]  # added for pretrain
        on_prey = np.where(np.all(self.predator_loc == self.prey_loc, axis=1))[0]  # commented for pretrain
        nb_predator_on_prey = on_prey.size

        if self.mode == 'cooperative':
            reward[on_prey] = self.POS_PREY_REWARD * nb_predator_on_prey
        elif self.mode == 'competitive':
            if nb_predator_on_prey:
                reward[on_prey] = self.POS_PREY_REWARD / nb_predator_on_prey
        elif self.mode == 'mixed':
            reward[on_prey] = self.PREY_REWARD
        else:
            raise RuntimeError("Incorrect mode, Available modes: [cooperative|competitive|mixed]")

        self.reached_prey[on_prey] = 1

        if np.all(self.reached_prey == 1) and self.mode == 'mixed':
            self.episode_over = True

        # Prey reward
        if nb_predator_on_prey == 0:
            reward[self.npredator:] = -1 * self.TIMESTEP_PENALTY
        else:
            # TODO: discuss & finalise
            reward[self.npredator:] = 0

        # Success ratio
        if self.mode != 'competitive':
            if nb_predator_on_prey == self.npredator:
                self.stat['success'] = 1
            else:
                self.stat['success'] = 0

        return reward

    def reward_terminal(self):
        return np.zeros_like(self._get_reward())


    def _onehot_initialization(self, a):
        ncols = self.vocab_size
        out = np.zeros(a.shape + (ncols,), dtype=int)
        out[self._all_idx(a, axis=2)] = 1
        return out

    def _all_idx(self, idx, axis):
        grid = np.ogrid[tuple(map(slice, idx.shape))]
        grid.insert(axis, idx)
        return tuple(grid)

    def render(self, mode='human', close=False):
        grid = np.zeros(self.BASE, dtype=object).reshape(self.dims)
        self.stdscr.clear()

        for p in self.predator_loc:
            if grid[p[0]][p[1]] != 0:
                grid[p[0]][p[1]] = str(grid[p[0]][p[1]]) + 'X'
            else:
                grid[p[0]][p[1]] = 'X'

        for p in self.prey_loc:
            if grid[p[0]][p[1]] != 0:
                grid[p[0]][p[1]] = str(grid[p[0]][p[1]]) + 'P'
            else:
                grid[p[0]][p[1]] = 'P'

        for row_num, row in enumerate(grid):
            for idx, item in enumerate(row):
                if item != 0:
                    if 'X' in item and 'P' in item:
                        self.stdscr.addstr(row_num, idx * 4, item.center(3), curses.color_pair(3))
                    elif 'X' in item:
                        self.stdscr.addstr(row_num, idx * 4, item.center(3), curses.color_pair(1))
                    else:
                        self.stdscr.addstr(row_num, idx * 4, item.center(3),  curses.color_pair(2))
                else:
                    self.stdscr.addstr(row_num, idx * 4, '0'.center(3), curses.color_pair(4))

        self.stdscr.addstr(len(grid), 0, '\n')
        self.stdscr.refresh()

    def seedset(self):
        pos = [-1, 1, 0]
        a = random.choice(pos)
        b = random.choice(pos)
        return a, b

    def exit_render(self):
        curses.endwin()
