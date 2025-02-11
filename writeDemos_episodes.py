import platform
print(platform.node())
import copy
import math
import os
import pickle as pkl
import sys

import time

from shutil import copyfile

import numpy as np

import hydra
import torch
import torch.nn as nn
import torch.nn.functional as F
import core.utils as utils
from core.logger import Logger
from core.replay_buffer_3 import ReplayBufferDoubleRewardEpisodes as ReplayBuffer
from core.video import VideoRecorder
import gym
import csv

from robosuite import make
from robosuite.wrappers.gym_wrapper import GymWrapper
from robosuite.controllers import load_controller_config

torch.backends.cudnn.benchmark = True

from custom_environments.indicatorboxBlock import IndicatorBoxBlock
from custom_environments.blocked_pick_place import BlockedPickPlace

def debug(str):
    print("\033[33mDEBUG: "+str+"\033[0m")

def make_env(cfg):
    env = make(
            cfg.environmentName,
            robots=["Panda"], #the robot
            controller_configs=load_controller_config(default_controller="OSC_POSITION"), #this decides which controller settings to use!
            has_renderer=False,
            has_offscreen_renderer=True,
            use_camera_obs=True,
            use_object_obs = True,
            reward_shaping=True,
            control_freq=20,
            horizon = cfg.horizon,
            camera_names="agentview",
            camera_heights=cfg.image_size,
            camera_widths=cfg.image_size
            )

    allmodlist = list(cfg.modalities)
    allmodlist.append(cfg.cameraName)
    env = GymWrapper(env, keys = allmodlist)
    ob_dict = env.env.reset()
    dims = 0
    for key in ob_dict:
        if key in cfg.modalities:
            dims += np.shape(ob_dict[key])[0]
    if cfg.stacked:
        cfg.agent.params.lowdim_dim = 10 * dims
        env = utils.FrameStack_Lowdim(env, cfg, k=3, l_k = 10, frameMode = 'cat', demo = True, audio = False)
    else:
        cfg.agent.params.lowdim_dim = dims
        env = utils.FrameStack_Lowdim(env, cfg, k=1, l_k = 1, frameMode = 'cat', demo = True, audio = False)

    return env


class Workspace(object):
    def __init__(self, cfg):
        self.work_dir = os.getcwd()
        print(f'workspace: {self.work_dir}')
        print("cuda status: ", torch.cuda.is_available())
        self.cfg = cfg

        utils.set_seed_everywhere(cfg.seed)
        self.device = torch.device(cfg.device)
        self.env = make_env(cfg)

        self.replay_buffer = ReplayBuffer(self.env.lowdim_space,
                                          self.env.observation_space.shape,
                                          self.env.action_space.shape,
                                          self.cfg.episodes,
                                          self.cfg.episodeLength,
                                          self.cfg.image_pad, self.device)


        self.video_recorder = VideoRecorder(
            self.work_dir if cfg.save_video else None)
        self.step = 0

    # dummy function to get a picture of the environment
    def record_env(self, iteration, cfg, record = False):
        self.env.reset()
        self.video_recorder.new_recorder_init(f'demo_' + str(iteration) + '.gif')
        for i in range(10):
            self.video_recorder.simple_record(self.env.render_highdim_list(256, 256, ["agentview", "birdview"]))
            self.env.step([0, 0, 0, 0])
        self.video_recorder.clean_up()
        return 1

    def single_demo_pick_place(self, iteration, cfg, record = False):
        # housekeeping varaibles
        assert cfg.environmentName == "BlockedPickPlace"
        episode, episode_reward, episode_step, done = 0, 0, 1, True
        raw_dict, lowdim, obs = self.env.reset()
        done = False
        episode_reward = 0
        episode_step = 0
        episode += 1
        status = 0
        gripperMagnitude = 1
        gripperStatus = -gripperMagnitude
        liftStatus = False
        destination = np.zeros(3)
        success = 0

        if record:
            self.video_recorder.new_recorder_init(f'demo_' + str(iteration) + '.gif')

        buffer_list = list()
        raw_dict, next_lowdim, next_obs, reward, done, info = self.env.step([0, 0, 0, gripperMagnitude])

        bin_pos = raw_dict['bin_pos']

        while self.step < cfg.episodeLength:
            cube_pos = raw_dict["cube_pos"]
            claw_pos = raw_dict["robot0_eef_pos"]
            gripper_pos = raw_dict["robot0_gripper_qpos"]
            status_dict = {0 : "sidereach", 1 : "sidestep", 3: "moveup", 4 : "positioning",
                          5 : "blockreach", 6 : "grabbing", 7 : "lifting", 8: "position", 9: "drop"}
            if status == 0: #reach to a predetermined position
                destination = [0, 0.08, 0.82]
            elif status == 1: #move to the side
                destination = cube_pos
                destination[1] += 0.01
            elif status == 3: #move up
                destination = claw_pos.copy()
                destination[2] = cube_pos[2] + 0.026
                gripperStatus = -gripperMagnitude
            elif status == 4: #move over cube
                destination = cube_pos
                destination[2] = cube_pos[2] + 0.026
                gripperStatus = -gripperMagnitude
            elif status == 5: #reach
                gripperStatus = -gripperMagnitude
                destination = cube_pos
            elif status == 6: #grab
                gripperStatus = gripperMagnitude
            elif status == 7: #lift
                liftStatus = True
                gripperStatus = gripperMagnitude
            elif status == 8:
                destination = bin_pos
                destination[2] = claw_pos[2] #keep altidude, move to center of bin
                liftStatus = False
                gripperStatus = gripperMagnitude
            elif status == 9:
                gripperStatus = -gripperMagnitude

            else:
                raise Exception("whoops!")

            displacement = destination - claw_pos

            # switchboard
            if np.linalg.norm(displacement) < 0.02 and status == 8:
                status = 9
            if claw_pos[2] > 0.95 and status == 7:
                status = 8
            if np.linalg.norm(gripper_pos) < 0.045 and status == 6: #used to be 0.04
                status = 7
            if np.linalg.norm(claw_pos - cube_pos) > 0.1 and status == 7: #close to lift
                # print("regrasping!") #uncomment for verbose output
                status = 0
            if np.linalg.norm(displacement) < 0.02 and status == 5: #plunge to close #used to be 0.01
                status = 6
            if np.linalg.norm(displacement) < 0.02 and status == 4: #approach to plunge
                status = 5
            if np.linalg.norm(displacement) < 0.02 and status == 3: #raise to approach
                status = 4
            if np.linalg.norm(raw_dict["gripper_force"]) > 1 and status == 1: #remove to raise
                # print("CONTACT") #uncomment for verbose output
                status = 3
            if np.linalg.norm(displacement) < 0.02 and status == 0: #reach next to the cube
                status = 1

            #scale, add randomness, and clip
            displacement = np.multiply(displacement, 5)
            displacement = [component + (np.random.rand() - 0.5) * 0.3 for component in displacement] #adding randomness
            displacement = [component if -1 <= component <= 1 else -1 if component < -1 else 1 for component in displacement]

            if not(liftStatus):
                action = np.append(displacement, gripperStatus)
            else:
                action = np.array([(np.random.rand() - 0.5) * 0.5, (np.random.rand() - 0.5) * 0.5, 1, gripperStatus])  #xy random during lift

            assert max(map(abs, action)) <= 1 #making sure we are within our action space
            raw_dict, next_lowdim, next_obs, reward, done, info = self.env.step(action)

            if record:
                self.video_recorder.simple_record(self.env.render_highdim_list(256, 256, ["agentview", "sideview"]))
            # allow infinite bootstrap
            done = float(done)
            done_no_max = 0 if episode_step + 1 == self.env._max_episode_steps else done
            episode_reward += reward

            buffer_list.append((lowdim, obs, action, reward,
                                (1.0 if self.step > cfg.sparseProp * cfg.episodeLength else 0.0), next_lowdim, next_obs, done, done_no_max))

            if self.step % 10 == 0:
                print(reward)
                print(status_dict[status])
            if reward > 0.99:
                success = 1
            obs = next_obs
            lowdim = next_lowdim
            episode_step += 1
            self.step += 1

        if record:
            self.video_recorder.clean_up()
        if success == 1:
            self.replay_buffer.add(buffer_list)

            print("****** ADDED ****** and we are at ", self.replay_buffer.idx)
        return success

    def single_demo_indicator_box(self, iteration, cfg, record = False):
        # housekeeping variables
        assert cfg.environmentName == "IndicatorBoxBlock"
        episode, episode_reward, episode_step, done = 0, 0, 1, True
        raw_dict, lowdim, obs = self.env.reset()
        done = False
        episode_reward = 0
        episode_step = 0
        episode += 1
        status = 0
        gripperMagnitude = 1
        gripperStatus = -gripperMagnitude
        liftStatus = False
        destination = np.zeros(3)
        success = 0

        if record:
            self.video_recorder.new_recorder_init(f'demo_' + str(iteration) + '.gif')

        buffer_list = list()
        raw_dict, next_lowdim, next_obs, reward, done, info = self.env.step([0, 0, 0, gripperMagnitude])
        initial_cube = raw_dict["cube_pos"]

        #determines the initial target location of the arm
        if initial_cube[1] < -0.1:
            target_y = -0.3
        elif initial_cube[1] < 0:
            target_y = -0.2
        elif initial_cube[1] < 0.1:
            target_y = -0.1
        else:
            target_y = 0


        while self.step < cfg.episodeLength:
            cube_pos = raw_dict["cube_pos"]
            claw_pos = raw_dict["robot0_eef_pos"]
            gripper_pos = raw_dict["robot0_gripper_qpos"]
            status_dict = {0 : "sidereach", 1 : "sidestep", 3: "moveup", 4 : "positioning",
                          5 : "blockreach", 6 : "grabbing", 7 : "lifting", 8: "HALT"} #for diagnostics
            if status == 0: #reach for cube
                destination = cube_pos
                destination[1] = target_y
            elif status == 1: #move to the side
                destination = cube_pos
                destination[1] += 0.11 #change back to 0.01
            elif status == 3: #move up
                destination = claw_pos.copy()
                destination[2] = cube_pos[2] + 0.03
                gripperStatus = -gripperMagnitude
            elif status == 4: #move over cube
                destination = cube_pos
                destination[2] = cube_pos[2] + 0.03
                gripperStatus = -gripperMagnitude
            elif status == 5: #reach
                gripperStatus = -gripperMagnitude
                destination = cube_pos
            elif status == 6: #grab
                gripperStatus = gripperMagnitude
            elif status == 7: #lift
                liftStatus = True
                gripperStatus = gripperMagnitude
            elif status == 8:
                destination = claw_pos
                liftStatus = False
            else:
                raise Exception("Unexpected status")
            displacement = destination - claw_pos
            if claw_pos[2] > 0.95 and status == 7:
                status = 8
            if np.linalg.norm(gripper_pos) < 0.045 and status == 6: #used to be 0.04
                status = 7
            if np.linalg.norm(claw_pos - cube_pos) > 0.1 and status == 7: #close to lift
                # print("regrasping!") #uncomment for verbose output
                status = 0
            if np.linalg.norm(displacement) < 0.02 and status == 5: #plunge to close #used to be 0.01
                status = 6
            if np.linalg.norm(displacement) < 0.02 and status == 4: #approach to plunge
                status = 5
            if np.linalg.norm(displacement) < 0.02 and status == 3: #raise to approach
                status = 4
            if np.linalg.norm(raw_dict["gripper_force"]) > 1 and status == 1: #remove to raise formerly 1.3
                # print("CONTACT") #uncomment for verbose output
                status = 3
            if np.linalg.norm(displacement) < 0.02 and status == 0: #reach next to the cube
                status = 1

            #scale, add randomness, and clip
            displacement = np.multiply(displacement, 5)
            displacement = [component + (np.random.rand() - 0.5) * 0.3 for component in displacement] #adding randomness
            displacement = [component if -1 <= component <= 1 else -1 if component < -1 else 1 for component in displacement]

            if not(liftStatus):
                action = np.append(displacement, gripperStatus)
            else:
                action = np.array([(np.random.rand() - 0.5) * 0.5, (np.random.rand() - 0.5) * 0.5, 1, gripperStatus]) #when lifting block, xy is random

            assert max(map(abs, action)) <= 1 #making sure we are within our action space

            raw_dict, next_lowdim, next_obs, reward, done, info = self.env.step(action)

            if record:
                self.video_recorder.simple_record(self.env.render_highdim_list(256, 256, ["agentview", "sideview"]))

            done = float(done)
            done_no_max = 0 if episode_step + 1 == self.env._max_episode_steps else done
            episode_reward += reward

            
            debug(f"lowdim.shape {lowdim.shape}\n obs.shape {obs.shape}\n action.shape {action.shape}\n reward.shape {reward}\n next_lowdim.shape {next_lowdim.shape}\n next_obs.shape {next_obs.shape}\n done {done}\n done_no_max {done_no_max}")
            buffer_list.append((lowdim, obs, action, reward,
                                (1.0 if self.step > cfg.sparseProp * cfg.episodeLength else 0.0), next_lowdim, next_obs, done, done_no_max))

            if self.step % 10 == 0:
                print(reward)
                print(status_dict[status])

            if reward > 0.99:
                success = 1
            obs = next_obs
            lowdim = next_lowdim
            episode_step += 1
            self.step += 1

        if record:
            self.video_recorder.clean_up()
        if success == 1:
            self.replay_buffer.add(buffer_list)
            print("****** ADDED ****** and we are at ", self.replay_buffer.idx)
        return success


    def run(self, cfg):
        counter = 0
        successes = 0
        func_dict = {"IndicatorBoxBlock": self.single_demo_indicator_box, "BlockedPickPlace": self.single_demo_pick_place}
        task = func_dict[cfg.environmentName]
        print(task)
        while successes < math.ceil(cfg.episodes):
            isSuccessful = task(counter, cfg, record = (counter % cfg.recordFrq == 0))
            counter += 1
            self.step = 0
            successes += isSuccessful
            print("\t", successes, " out of ", counter)
        print("Saving demos...")
        pkl.dump(self.replay_buffer, open( "demos.pkl", "wb" ), protocol=4 )
        print("Demos saved")


@hydra.main(config_path='writeDemos_episodes.yaml', strict=True)
def main(cfg):
    from writeDemos_episodes import Workspace as W
    workspace = W(cfg)
    workspace.run(cfg)

if __name__ == '__main__':
    main()

