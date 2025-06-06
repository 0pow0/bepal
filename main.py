import sys
import time
import signal
import argparse
import os,subprocess
import numpy as np
import torch
# import visdom
import data
from models import *
from comm import CommNetMLP
from utils import *
from action_utils import parse_action_args
from trainer import Trainer
from multi_processing import MultiProcessTrainer
import wandb

torch.utils.backcompat.broadcast_warning.enabled = True
torch.utils.backcompat.keepdim_warning.enabled = True

torch.set_default_tensor_type('torch.DoubleTensor')
parser = argparse.ArgumentParser(description='PyTorch RL trainer')
# training
# note: number of steps per epoch = epoch_size X batch_size x nprocesses
parser.add_argument('--num_epochs', default=100, type=int,
                    help='number of training epochs')
parser.add_argument('--epoch_size', type=int, default=10,
                    help='number of update iterations in an epoch')
parser.add_argument('--batch_size', type=int, default=500,
                    help='number of steps before each update (per thread)')
parser.add_argument('--nprocesses', type=int, default=16,
                    help='How many processes to run')
# model
parser.add_argument('--hid_size', default=64, type=int,
                    help='hidden layer size')
parser.add_argument('--recurrent', action='store_true', default=False,
                    help='make the model recurrent in time')
# optimization
parser.add_argument('--gamma', type=float, default=1.0,
                    help='discount factor')
parser.add_argument('--tau', type=float, default=1.0,
                    help='gae (remove?)')
parser.add_argument('--seed', type=int, default=-1,
                    help='random seed. Pass -1 for random seed') # TODO: works in thread?
parser.add_argument('--normalize_rewards', action='store_true', default=False,
                    help='normalize rewards in each batch')
parser.add_argument('--lrate', type=float, default=0.001,
                    help='learning rate')
parser.add_argument('--entr', type=float, default=0,
                    help='entropy regularization coeff')
parser.add_argument('--value_coeff', type=float, default=0.01,
                    help='coeff for value loss term')
# environment
parser.add_argument('--env_name', default="Cartpole",
                    help='name of the environment to run')
parser.add_argument('--max_steps', default=20, type=int,
                    help='force to end the game after this many steps')
parser.add_argument('--nactions', default='1', type=str,
                    help='the number of agent actions (0 for continuous). Use N:M:K for multiple actions')
parser.add_argument('--action_scale', default=1.0, type=float,
                    help='scale action output from model')
parser.add_argument('--obstacles', default=10, type=int,
                    help='number of obstacles in the environment')
# other
parser.add_argument('--plot', action='store_true', default=False,
                    help='plot training progress')
parser.add_argument('--plot_env', default='main', type=str,
                    help='plot env name')
parser.add_argument('--save', default='', type=str,
                    help='save the model after training')
parser.add_argument('--save_every', default=0, type=int,
                    help='save the model after every n_th epoch')
parser.add_argument('--load', default='', type=str,
                    help='load the model')
parser.add_argument('--display', action="store_true", default=False,
                    help='Display environment state')


parser.add_argument('--random', action='store_true', default=False,
                    help="enable random model")

# CommNet specific args
parser.add_argument('--commnet', action='store_true', default=False,
                    help="enable commnet model")
parser.add_argument('--ic3net', action='store_true', default=False,
                    help="enable commnet model")
parser.add_argument('--nagents', type=int, default=1,
                    help="Number of agents (used in multiagent)")
parser.add_argument('--comm_mode', type=str, default='avg',
                    help="Type of mode for communication tensor calculation [avg|sum]")
parser.add_argument('--comm_passes', type=int, default=1,
                    help="Number of comm passes per step over the model")
parser.add_argument('--comm_mask_zero', action='store_true', default=False,
                    help="Whether communication should be there")
parser.add_argument('--mean_ratio', default=1.0, type=float,
                    help='how much coooperative to do? 1.0 means fully cooperative')
parser.add_argument('--rnn_type', default='MLP', type=str,
                    help='type of rnn to use. [LSTM|MLP]')
parser.add_argument('--detach_gap', default=10000, type=int,
                    help='detach hidden state and cell state for rnns at this interval.'
                    + ' Default 10000 (very high)')
parser.add_argument('--comm_init', default='uniform', type=str,
                    help='how to initialise comm weights [uniform|zeros]')
parser.add_argument('--hard_attn', default=False, action='store_true',
                    help='Whether to use hard attention: action - talk|silent')
parser.add_argument('--comm_action_one', default=False, action='store_true',
                    help='Whether to always talk, sanity check for hard attention.')
parser.add_argument('--advantages_per_action', default=False, action='store_true',
                    help='Whether to multipy log porb for each chosen action with advantages')
parser.add_argument('--share_weights', default=False, action='store_true',
                    help='Share weights for hops')
# Add wandb arguments to parser
parser.add_argument('--wandb_run', type=str, default='debug',
                   help='Weights & Biases run name')
# Add communication value 
parser.add_argument('--comm_baseline_path', type=str, default=None,
                   help='Unlearned policy ckpt path w/o communication')

parser.add_argument('--objective', choices=['original', 'unlearn', 'advantage_baseline'], default='original',
                    type=str, help='type of objectives to use')

init_args_for_env(parser)
args = parser.parse_args()

# Initialize wandb
wandb.init(project='bepal', config=args, name=f"{args.wandb_run}-{time.strftime('%Y%m%d-%H%M%S')}")
wandb.config.update(args)  # Add all arguments to config

device = torch.device('cuda:0' if torch.cuda.is_available else 'cpu')

if args.ic3net:
    args.commnet = 1
    args.hard_attn = 1
    args.mean_ratio = 0

    # For TJ set comm action to 1 as specified in paper to showcase
    # importance of individual rewards even in cooperative games
    if args.env_name == "traffic_junction":
        args.comm_action_one = True
# Enemy comm
args.nfriendly = args.nagents
if hasattr(args, 'enemy_comm') and args.enemy_comm:
    if hasattr(args, 'nenemies'):
        args.nagents += args.nenemies
    else:
        raise RuntimeError("Env. needs to pass argument 'nenemy'.")

env = data.init(args.env_name, args, False)

num_inputs = env.observation_dim
args.num_actions = env.num_actions

# Multi-action
if not isinstance(args.num_actions, (list, tuple)): # single action case
    args.num_actions = [args.num_actions]
args.dim_actions = env.dim_actions
args.num_inputs = num_inputs

# Hard attention
if args.hard_attn and args.commnet:
#    # add comm_action as last dim in actions
    args.num_actions = [*args.num_actions, 2]
    args.dim_actions = env.dim_actions + 1

# Recurrence
if args.commnet and (args.recurrent or args.rnn_type == 'LSTM'):
    args.recurrent = True
    args.rnn_type = 'LSTM'


parse_action_args(args)

if args.seed == -1:
    args.seed = np.random.randint(0,10000)
torch.manual_seed(args.seed)

print(args, flush=True)

#device = torch.device("cpu") #"cuda:0" if torch.cuda.is_available() else
''''''

if args.commnet:
    policy_net = CommNetMLP(args, num_inputs)#.to(device)
    wocomm_baseline = None
    if args.objective == "advantage_baseline":
        wocomm_baseline = CommNetMLP(args, num_inputs)
        d = torch.load(args.comm_baseline_path)
        wocomm_baseline.load_state_dict(d['policy_net'])
     
     # d = torch.load('/home/qhuang/ppgcn/result/gcn_ppnode_agent_node_obs_12k_5994')
     # d = torch.load('/home/qhuang/decoder/result/model_tmc+map_7992')
     # log.clear()
     # policy_net.load_state_dict(d['policy_net'])
     # policy_net.eval()
elif args.random:
    policy_net = Random(args, num_inputs)
elif args.recurrent:
    policy_net = RNN(args, num_inputs)
else:
    policy_net = MLP(args, num_inputs)

if not args.display:
    display_models([policy_net])

wandb.watch(policy_net, log='all', log_freq=10)

# share parameters among threads, but not gradients
for p in policy_net.parameters():
    p.data.share_memory_()

if args.nprocesses > 1:
    trainer = MultiProcessTrainer(args, lambda: Trainer(args, policy_net, data.init(args.env_name, args)))
else:
    trainer = Trainer(args, policy_net, data.init(args.env_name, args), wocomm_baseline)

disp_trainer = Trainer(args, policy_net, data.init(args.env_name, args, False))
disp_trainer.display = True
def disp():
    x = disp_trainer.get_episode()

log = dict()
log['epoch'] = LogField(list(), False, None, None)
log['reward'] = LogField(list(), True, 'epoch', 'num_episodes')
log['enemy_reward'] = LogField(list(), True, 'epoch', 'num_episodes')
log['success'] = LogField(list(), True, 'epoch', 'num_episodes')
log['steps_taken'] = LogField(list(), True, 'epoch', 'num_episodes')
log['add_rate'] = LogField(list(), True, 'epoch', 'num_episodes')
log['comm_action'] = LogField(list(), True, 'epoch', 'num_steps')
log['enemy_comm'] = LogField(list(), True, 'epoch', 'num_steps')
log['value_loss'] = LogField(list(), True, 'epoch', 'num_steps')
log['action_loss'] = LogField(list(), True, 'epoch', 'num_steps')
log['entropy'] = LogField(list(), True, 'epoch', 'num_steps')
log['map_loss'] = LogField(list(), True, 'epoch', 'num_steps')
log['loss'] = LogField(list(), True, 'epoch', 'num_steps')
log['value_loss_g'] = LogField(list(), True, 'epoch', 'num_steps')


if args.plot:
    vis = visdom.Visdom(env=args.plot_env)

def run(num_epochs):
    global_succ = 0
    for ep in range(num_epochs):
        # epoch_begin_time = time.time()
        stat = dict()
        for n in range(args.epoch_size):
            if n == args.epoch_size - 1 and args.display:
                trainer.display = True
            s = trainer.train_batch(ep)
            trainer.scheduler.step()
            merge_stat(s, stat)
            trainer.display = False

            idx = ep*args.epoch_size + (n+1)
        # epoch_time = time.time() - epoch_begin_time
        epoch = len(log['epoch'].data) + 1
        for k, v in log.items():
            if k == 'epoch':
                v.data.append(epoch)
            else:
                if k in stat and v.divide_by is not None and stat[v.divide_by] > 0:
                    stat[k] = stat[k] / stat[v.divide_by]
                v.data.append(stat.get(k, 0))

        np.set_printoptions(precision=2)
        # stat['min_steps'] = stat['min_steps'] / stat['num_episodes']
        #stat['map_loss'] = stat['map_loss'] / stat['num_episodes'] , epoch_time
        print('Epoch {}\tReward {}'.format(
                epoch, stat['reward']), flush=True)

        if 'enemy_reward' in stat.keys():
            print('Enemy-Reward: {}'.format(stat['enemy_reward']), flush=True)
        if 'add_rate' in stat.keys():
            print('Add-Rate: {:.2f}'.format(stat['add_rate']), flush=True)
        if 'success' in stat.keys():
            print('Success: {:.2f}'.format(stat['success']), flush=True)
        if 'steps_taken' in stat.keys():
            print('Steps-taken: {:.2f}'.format(stat['steps_taken']), flush=True)
        if 'comm_action' in stat.keys():
            print('Comm-Action: {}'.format(stat['comm_action']), flush=True)
        if 'enemy_comm' in stat.keys():
            print('Enemy-Comm: {}'.format(stat['enemy_comm']), flush=True)
        if 'map_loss' in stat.keys():
            print('Map loss: {}'.format(stat['map_loss']), flush=True)
        if 'loss' in stat.keys():
            print('loss: {}'.format(stat['loss']), flush=True)
        if 'action_loss' in stat.keys():
            print('action_loss: {}'.format(stat['action_loss']), flush=True)
        if 'comm_loss' in stat.keys():
            print('comm_loss: {}'.format(stat['comm_loss']), flush=True)
        if 'value_loss' in stat.keys():
            print('value_loss: {}'.format(stat['value_loss']), flush=True)
        if 'value_loss_g' in stat.keys():
            print('gloable value loss: {}'.format(stat['value_loss_g']), flush=True)
                # After printing metrics, add wandb logging
        wandb.log({
            'epoch': epoch,
            'reward': np.mean(stat['reward']),
            'enemy_reward': stat.get('enemy_reward', 0),
            'success': stat.get('success', 0),
            'steps_taken': stat.get('steps_taken', 0),
            'comm_action': np.mean(stat.get('comm_action', 0)),
            'map_loss': stat.get('map_loss', 0),
            'loss': stat.get('loss', 0),
            'action_loss': stat.get('action_loss', 0),
            'comm_loss': stat.get('comm_loss', 0),
            'value_loss': stat.get('value_loss', 0),
            'value_loss_g': stat.get('value_loss_g', 0),
            'entropy': stat.get('entropy', 0),
            'learning_rate': trainer.scheduler.get_last_lr()[0]
        }, step=epoch)


        if args.plot:
            for k, v in log.items():
                if v.plot and len(v.data) > 0:
                    vis.line(np.asarray(v.data), np.asarray(log[v.x_axis].data[-len(v.data):]),
                    win=k, opts=dict(xlabel=v.x_axis, ylabel=k))

        if args.save_every and ep and args.save != '' and ep % args.save_every == 0:
            # fname, ext = args.save.split('.')
            # save(fname + '_' + str(ep) + '.' + ext)
            save(args.save + '_' + str(ep))

        # if args.save != '' and stat['success'] > global_succ:
        #     global_succ = stat['success']
        #     save(args.save+'_succ_ml')

    save(args.save+'_succ_ml')


def save(path):
    current_path =  os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + ".")
    full_path = current_path + path
    
    d = dict()
    d['policy_net'] = policy_net.state_dict()
    d['log'] = log
    d['trainer'] = trainer.state_dict()

    print(full_path)
    torch.save(d, full_path)
    #torch.save(policy_net.mapdecode.state_dict(), path)

    # Log model to wandb
    wandb.save(full_path)  # Or use wandb.Artifact for more control
    print(f"Saved model to {full_path} and logged to W&B")

def load(path):
    current_path =  os.path.abspath(os.path.dirname(os.path.abspath(__file__)) + os.path.sep + ".")
    d = torch.load(current_path+path)
    # log.clear()
    policy_net.load_state_dict(d['policy_net'])
    log.update(d['log'])
    trainer.load_state_dict(d['trainer'])

def signal_handler(signal, frame):
        print('You pressed Ctrl+C! Exiting gracefully.')
        if args.display:
            env.end_display()
        sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

if args.load != '':
    load(args.load)

if args.objective == "original":
    for param in policy_net.parameters():
        param.requires_grad = True
else:
    for param in policy_net.parameters():
        param.requires_grad = False
    # 解冻 heads[0] 的参数
    for param in policy_net.heads[0].parameters():
        param.requires_grad = True 
    for param in policy_net.heads[1].parameters():
        param.requires_grad = True 
    for param in policy_net.value_head.parameters():
        param.requires_grad = True

print("Policy parameters:")
for name, param in policy_net.named_parameters():
    print(f"{name}: {param.requires_grad}")

if args.objective == "advantage_baseline":
    # freeze all layers of w/o communication policy
    for param in wocomm_baseline.parameters():
        param.requires_grad = False

    print("\nW/o comm baseline parameters:")
    for name, param in wocomm_baseline.named_parameters():
        print(f"{name}: {param.requires_grad}")

run(args.num_epochs)
if args.display:
    env.end_display()

# if args.save != '':
#     save(args.save)

wandb.finish()

if sys.flags.interactive == 0 and args.nprocesses > 1:
    trainer.quit()
    import os
    os._exit(0)
