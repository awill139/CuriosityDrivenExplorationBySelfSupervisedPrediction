import gym
gym.logger.set_level(40)

import argparse, pickle

import numpy as np
import time

import torch
import torch.nn.functional as F

from IPython.display import clear_output
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.switch_backend('agg')

from timeit import default_timer as timer
from datetime import timedelta
import os
import glob

from utils.wrappers import make_env_a2c_smb
from utils.plot import tb_plot_from_monitor
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv

from utils.hyperparameters import PolicyConfig

#NOTE: tmp
from torch.utils.tensorboard import SummaryWriter

parser = argparse.ArgumentParser(description='RL')
parser.add_argument('--env-name', default='SuperMarioBros-1-1-v0',
					help='environment to train on (default: SuperMarioBros-1-1-v0)')
parser.add_argument('--dim', type=int, default=84,
                    help='Dimensionality (h and w) of preprocessed frames (default: 42)')
parser.add_argument('--reward-type', type=str, default='none',
                    choices=('none', 'sparse', 'dense'),
					help='Type of reward. Choices = {none, sparse, dense}. (default: none))')
parser.add_argument('--stack-frames', type=int, default=4,
					help='Number of frames to stack (default: 4)')
parser.add_argument('--adaptive-repeat', nargs='+', type=int, default=[4],
                    help='Possible action repeat values (default: [4])')
parser.add_argument('--sticky-actions', type=float, default=0.,
                    help='Sticky action probability. I.e. the probability that input is ignored and the previous action is repeated (default: 0.)')
parser.add_argument('--algo', default='icma2c',
					help='algorithm to use: icma2c | a2c')
parser.add_argument('--print-threshold', type=int, default=1,
					help='print progress and plot every print-threshold timesteps (default: 1)')
parser.add_argument('--tb-dump', type=int, default=30,
					help='Seconds between dumps of gym.monitor data (env reward and ep length) to tensorboard (default: 30)')
parser.add_argument('--lr', type=float, default=1e-4,
					help='learning rate (default: 1e-4)')
parser.add_argument('--gamma', type=float, default=0.9,
					help='discount factor for rewards (default: 0.99)')
parser.add_argument('--num-frames', type=int, default=5e6,
					help='number of frames to train (default: 1e6)')
parser.add_argument('--num-steps', type=int, default=50,
					help='number of forward steps in A2C (default: 50)')
parser.add_argument('--num-processes', type=int, default=6,
					help='how many training CPU processes to use (default: 6)')
parser.add_argument('--value-loss-coef', type=float, default=1.0,
					help='value loss coefficient (default: 1.0)')
parser.add_argument('--entropy-coef', type=float, default=0.01,
					help='entropy term coefficient (default: 0.01)')
parser.add_argument('--max-grad-norm', type=float, default=40.0,
					help='max norm of gradients (default: 40.0)')
parser.add_argument('--disable-gae', action='store_false', default=True,
					help='disable generalized advantage estimation')
parser.add_argument('--tau', type=float, default=1.0,
					help='gae parameter (default: 1.0)')
parser.add_argument('--recurrent-policy', action='store_true', default=False,
					help='Activate recurrent policy')
parser.add_argument('--gru-size', type=int, default=512,
					help='number of output units for main gru (default: 512)')
parser.add_argument('--icm-loss-beta', type=float, default=0.2,
					help='Weight used by ICM to trade off forward/backward model optim (default: 0.2)')
parser.add_argument('--icm-prediction-beta', type=float, default=0.2,
					help='Weight used by ICM on intrinsic reward calc (default: 0.2)')
parser.add_argument('--icm-lambda', type=float, default=1.0,
					help='Weight placed by ICM of PG loss (default: 1.0)')
parser.add_argument('--icm-minibatches', type=int, default=1,
                    help='Number of minibatches to use in icm forward model. Needed due to large memory complexity (default: 64)')
parser.add_argument('--inference', action='store_true', default=False,
					help='Inference saved model')
parser.add_argument('--render', action='store_true', default=False,
                    help='Render the inference epsiode (default: False')

args = parser.parse_args()

if args.algo == 'icma2c':
    from agents.ICM_A2C import Model
elif args.algo == 'a2c':
    from agents.A2C import Model
else:
    print("INVALID ALGORITHM. ABORT.")
    exit()
    
if args.recurrent_policy:
    model_architecture = 'recurrent/'
else:
    model_architecture = 'feedforward/'

config = PolicyConfig()
config.algo = args.algo
config.env_id = args.env_name

#icm
config.icm_loss_beta = args.icm_loss_beta
config.icm_prediction_beta = args.icm_prediction_beta
config.icm_lambda = args.icm_lambda
config.icm_minibatches = args.icm_minibatches

#preprocessing
config.stack_frames = args.stack_frames
config.reward_type = args.reward_type
config.adaptive_repeat = args.adaptive_repeat #adaptive repeat

#Recurrent control
config.recurrent_policy_grad = args.recurrent_policy
config.gru_size = args.gru_size

#a2c control
config.num_agents=args.num_processes
config.rollout=args.num_steps
config.USE_GAE = args.disable_gae
config.gae_tau = args.tau

#misc agent variables
config.GAMMA=args.gamma
config.LR=args.lr
config.entropy_loss_weight=args.entropy_coef
config.value_loss_weight=args.value_loss_coef
config.grad_norm_max = args.max_grad_norm

config.MAX_FRAMES=int(args.num_frames / config.num_agents / config.rollout)

def save_config(config, base_dir):
    tmp_device = config.device
    config.device = None
    pickle.dump(config, open(os.path.join(base_dir, 'config.dump'), 'wb'))
    config.device = tmp_device

def train(config):
    max_dists = []
    base_dir = os.path.join('./results/', args.algo, model_architecture, config.env_id)
    try:
        os.makedirs(base_dir)
    except OSError:
        files = glob.glob(os.path.join(base_dir, '*.*'))
        for f in files:
            os.remove(f)

    best_dir = os.path.join(base_dir, 'best/')
    try:
        os.makedirs(best_dir)
    except OSError:
        files = glob.glob(os.path.join(best_dir, '*.dump'))
        for f in files:
            os.remove(f)
    
    log_dir = os.path.join(base_dir, 'logs/')
    try:
        os.makedirs(log_dir)
    except OSError:
        files = glob.glob(os.path.join(log_dir, '*.csv'))+glob.glob(os.path.join(log_dir, '*.png'))
        for f in files:
            os.remove(f)
            
    model_dir = os.path.join(base_dir, 'saved_model/')
    try:
        os.makedirs(model_dir)
    except OSError:
        files = glob.glob(os.path.join(model_dir, '*.dump'))
        for f in files:
            os.remove(f)

    tb_dir = os.path.join(base_dir, 'runs/')
    try:
        os.makedirs(tb_dir)
    except OSError:
        files = glob.glob(os.path.join(tb_dir, '*.*'))
        for f in files:
            os.remove(f)

    #NOTE: tmp
    writer = SummaryWriter(log_dir=os.path.join(base_dir, 'runs'))
    
    #save configuration for later reference
    save_config(config, base_dir)

    seed = np.random.randint(0, int(1e6))

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)

    envs = [make_env_a2c_smb(config.env_id, seed, i, log_dir, dim=args.dim, stack_frames=config.stack_frames, adaptive_repeat=config.adaptive_repeat, reward_type=config.reward_type, sticky=args.sticky_actions) for i in range(config.num_agents)]
    envs = SubprocVecEnv(envs)

    model = Model(env=envs, config=config, log_dir=base_dir, static_policy=args.inference, tb_writer=writer)

    obs = envs.reset()
    
    obs = torch.from_numpy(obs.astype(np.float32)).to(config.device)

    model.config.rollouts.observations[0].copy_(obs)
    
    episode_rewards = np.zeros(config.num_agents, dtype=np.float)
    final_rewards = np.zeros(config.num_agents, dtype=np.float)

    start = timer()
    
    last_log = timer()
    last_reward_logged = 0

    print_threshold = args.print_threshold

    max_dist = np.zeros(config.num_agents)
    all_time_max = 0
    last_10 = []
    
    for frame_idx in range(1, config.MAX_FRAMES+1):
        for step in range(config.rollout):
            
            with torch.no_grad():
                values, actions, action_log_prob, states = model.get_action(
                                                            model.config.rollouts.observations[step],
                                                            model.config.rollouts.states[step],
                                                            model.config.rollouts.masks[step])
            
            cpu_actions = actions.view(-1).cpu().numpy()
    
            obs, reward, done, info = envs.step(cpu_actions)

            obs = torch.from_numpy(obs.astype(np.float32)).to(config.device)

            #agent rewards
            episode_rewards += reward
            masks = 1. - done.astype(np.float32)
            final_rewards *= masks
            final_rewards += (1. - masks) * episode_rewards
            episode_rewards *= masks

            for index, inf in enumerate(info):
                if inf['x_pos'] < 60000: #there's a simulator glitch? Ignore this value
                    max_dist[index] = np.max((max_dist[index], inf['x_pos']))
                
                if done[index]:
                    #model.save_generic_stat(max_dist[index], (frame_idx-1)*config.rollout*config.num_agents+step*config.num_agents+index, 'max_dist')
                    
                    #NOTE: tmp
                    writer.add_scalar('Performance/Max Distance', max_dist[index], (frame_idx-1)*config.rollout*config.num_agents+step*config.num_agents+index)
                    writer.add_scalar('Performance/Agent Reward', final_rewards[index], (frame_idx-1)*config.rollout*config.num_agents+step*config.num_agents+index)
                    
                    last_10.append(inf['x_pos'])
                    if len(last_10) > 10:
                        last_10.pop(0)
                        if np.mean(last_10) >= all_time_max:
                            all_time_max = np.mean(last_10)
                            model.save_w(best=True)
            max_dist*=masks

            rewards = torch.from_numpy(reward.astype(np.float32)).view(-1, 1).to(config.device)
            masks = torch.from_numpy(masks).to(config.device).view(-1, 1)

            obs *= masks.view(-1, 1, 1, 1)

            model.config.rollouts.insert(obs, states, actions.view(-1, 1), action_log_prob, values, rewards, masks)
            
        with torch.no_grad():
            next_value = model.get_values(model.config.rollouts.observations[-1],
                                model.config.rollouts.states[-1],
                                model.config.rollouts.masks[-1])
            
        value_loss, action_loss, dist_entropy, dynamics_loss = model.update(model.config.rollouts, next_value, frame_idx*config.rollout*config.num_agents)
        
        model.config.rollouts.after_update()

        if frame_idx % print_threshold == 0:
            #save_model
            if frame_idx % (print_threshold*10) == 0:
                model.save_w()
            
            #print
            end = timer()
            total_num_steps = (frame_idx) * config.num_agents * config.rollout
            print("Updates {}, num timesteps {}, FPS {}, max distance {:.1f}, mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}, entropy {:.5f}, val loss {:.5f}, pol loss {:.5f}, dyn loss {:.5f}".
                format(frame_idx, total_num_steps,
                       int(total_num_steps*np.mean(config.adaptive_repeat) / (end - start)),
                       np.mean(max_dist),
                       np.mean(final_rewards),
                       np.median(final_rewards),
                       np.min(final_rewards),
                       np.max(final_rewards), dist_entropy,
                       value_loss, action_loss, dynamics_loss))
            max_dists.append(np.mean(max_dist))
            if timer() - last_log > args.tb_dump:
                last_log = timer()
                tb_plot_from_monitor(writer, log_dir, np.mean(config.adaptive_repeat), last_reward_logged, 'reward')
                last_reward_logged = tb_plot_from_monitor(writer, log_dir, np.mean(config.adaptive_repeat), last_reward_logged, 'episode length')
    
    tb_plot_from_monitor(writer, log_dir, np.mean(config.adaptive_repeat), last_reward_logged, 'reward')
    tb_plot_from_monitor(writer, log_dir, np.mean(config.adaptive_repeat), last_reward_logged, 'episode length')

    model.save_w()
    envs.close()

    with open('curiousity2.p', 'wb') as f:
        pickle.dump(max_dists, f)

    lin = np.linspace(0, 5000000, 16666)
    plt.plot(lin, max_dists)
    plt.xlabel('Iterations')
    plt.ylabel('Progress')
    plt.savefig('curiousity_only_a2c.png')
    
def test(config):
    base_dir = os.path.join('./results/', args.algo, model_architecture, config.env_id)
    log_dir = os.path.join(base_dir, 'logs/')
    model_dir = os.path.join(base_dir, 'saved_model/')

    seed = np.random.randint(0, int(1e6))

    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)

    env = [make_env_a2c_smb(config.env_id, seed, config.num_agents+1, log_dir, dim=args.dim, stack_frames=config.stack_frames, adaptive_repeat=config.adaptive_repeat, reward_type=config.reward_type, sticky=args.sticky_actions, vid=args.render, base_dir=base_dir)]
    env = SubprocVecEnv(env)


    model = Model(env=env, config=config, log_dir=base_dir, static_policy=args.inference)
    model.load_w()

    obs = env.reset()
    
    if args.render:
        env.render()
    
    obs = torch.from_numpy(obs.astype(np.float32)).to(config.device)
    state = model.config.rollouts.states[0, 0].view(1, -1)
    mask = model.config.rollouts.masks[0, 0].view(1, -1)
    
    episode_rewards = np.zeros(1, dtype=np.float)
    final_rewards = np.zeros(1, dtype=np.float)

    start=timer()

    print_threshold = args.print_threshold

    max_dist = np.zeros(1, dtype=np.float)

    done = False
    tstep=0
    while not done:
        tstep+=1
        with torch.no_grad():
                value, action, action_log_prob, state = model.get_action(obs, state, mask)
            
        cpu_action = action.view(-1).cpu().numpy()
        obs, reward, done, info = env.step(cpu_action)

        if args.render:
            env.render()

        obs = torch.from_numpy(obs.astype(np.float32)).to(config.device)

        episode_rewards += reward
        mask = 1. - done.astype(np.float32)
        final_rewards += (1. - mask) * episode_rewards

        for index, inf in enumerate(info):
            if inf['x_pos'] < 60000: #there's a simulator glitch? Ignore this value
                max_dist[index] = np.max((max_dist[index], inf['x_pos']))

        mask = torch.from_numpy(mask).to(config.device).view(-1, 1)
        
    #print
    end = timer()
    total_num_steps = tstep
    print("Num timesteps {}, FPS {}, Distance {:.1f}, Reward {:.1f}".
        format(total_num_steps,
                int(total_num_steps / (end - start)),
                np.mean(max_dist),
                np.mean(final_rewards)))
    env.close()
            
    
if __name__=='__main__':
    toc = time.time()
    if not args.inference:
        train(config)
    else:
        test(config)
    tic = time.time()
    print('seconds: ', tic - toc)
    print('minutes: ', (tic - toc) / 60.0)
    print('hours: ', (tic - toc) / 3600.0)
