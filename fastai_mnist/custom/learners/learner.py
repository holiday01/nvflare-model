# Copyright (c) 2021, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import copy
import os

import numpy as np
import pandas as pd
from PIL import Image
import torch
import torch.optim as optim
from networks.nets import *
from torch.utils.tensorboard import SummaryWriter
from torchvision import transforms

from nvflare.apis.dxo import DXO, DataKind, MetaKey, from_shareable
from nvflare.apis.fl_constant import FLContextKey, ReturnCode
from nvflare.apis.fl_context import FLContext
from nvflare.apis.shareable import ReservedHeaderKey, Shareable, make_reply
from nvflare.apis.signal import Signal
from nvflare.app_common.abstract.learner_spec import Learner
from nvflare.app_common.abstract.model import ModelLearnableKey
from nvflare.app_common.app_constant import AppConstants, ModelName, ValidateType
from nvflare.app_common.pt.pt_fedproxloss import PTFedProxLoss
from fastai.vision.all import untar_data, get_image_files, ImageDataLoaders, Learner as fastai_L, verify_images, aug_transforms, Normalize, imagenet_stats,  ToTensor, Resize
from fastai.vision.all import untar_data, DataBlock, Normalize, URLs, ImageBlock, imagenet_stats, CrossEntropyLossFlat, Datasets, PILImageBW
from fastai.data.transforms import RandomSplitter, RegexLabeller, GrandparentSplitter, parent_label, Categorize, IntToFloatTensor
from fastai.vision.augment import RandomResizedCrop
from fastai.medical.imaging import PILDicom, CategoryBlock
import timm


class Learner(Learner):
    def __init__(
        self,
        dataset_root: str = "",
        aggregation_epochs: int = 1,
        train_task_name: str = AppConstants.TASK_TRAIN,
        submit_model_task_name: str = AppConstants.TASK_SUBMIT_MODEL,
        lr: float = 1e-2,
        fedproxloss_mu: float = 0.0,
        central: bool = False,
        analytic_sender_id: str = "analytic_sender",
    ):
        super().__init__()
        # trainer init happens at the very beginning, only the basic info regarding the trainer is set here
        # the actual run has not started at this point
        self.dataset_root = dataset_root
        self.aggregation_epochs = aggregation_epochs
        self.train_task_name = train_task_name
        self.lr = lr
        self.fedproxloss_mu = fedproxloss_mu
        self.submit_model_task_name = submit_model_task_name
        self.best_acc = 0.0
        self.central = central
        
        self.writer = None
        self.analytic_sender_id = analytic_sender_id

        # Epoch counter
        self.epoch_of_start_time = 0
        self.epoch_global = 0
        
    def initialize(self, parts: dict, fl_ctx: FLContext):
        # when the run starts, this is where the actual settings get initialized for trainer

        # Set the paths according to fl_ctx
        print("==============init=============")
        self.app_root = fl_ctx.get_prop(FLContextKey.APP_ROOT)
        fl_args = fl_ctx.get_prop(FLContextKey.ARGS)
        self.client_id = fl_ctx.get_identity_name()
        self.log_info(
            fl_ctx,
            f"Client {self.client_id} initialized at \n {self.app_root} \n with args: {fl_args}",
        )

        self.local_model_file = os.path.join(self.app_root, "local_model.pt")
        self.best_local_model_file = os.path.join(self.app_root, "best_local_model.pt")

        # Select local TensorBoard writer or event-based writer for streaming
        self.writer = parts.get(self.analytic_sender_id)  # user configured config_fed_client.json for streaming
        if not self.writer:  # use local TensorBoard writer only
            self.writer = SummaryWriter(self.app_root)
        
        # set the training-related parameters
        # can be replaced by a config-style block
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.criterion = CrossEntropyLossFlat()
        print("load_model")
        self.model = SimpleCNN().to(self.device)
        
        if self.fedproxloss_mu > 0:
            self.log_info(fl_ctx, f"using FedProx loss with mu {self.fedproxloss_mu}")
            self.criterion_prox = PTFedProxLoss(mu=self.fedproxloss_mu)

        # Set dataset
        print("loader")
        path = untar_data(URLs.MNIST)
        items = get_image_files(path)
        splits = GrandparentSplitter(train_name='training', valid_name='testing')
        splits = splits(items)
        dsrc = Datasets(items, tfms=[[PILImageBW.create], [parent_label, Categorize]],splits=splits)
        gpu_tfms = [IntToFloatTensor(), Normalize()]
        tfms = [ToTensor()]
        self.train_loader = dsrc.dataloaders(bs=512, after_item=tfms, after_batch=gpu_tfms, num_workers = 4)
        self.fastai_nnet = fastai_L(self.train_loader, self.model, loss_func=self.criterion)
        
        #self.valid_loader = self.train_loader[1]
        

    def finalize(self, fl_ctx: FLContext):
        # collect threads, close files here
        pass

    def local_train(self, fl_ctx, fastai_nnet , model_global, abort_signal: Signal, val_freq: int = 0):
        if abort_signal.triggered:
            return
        #self.log_info(fl_ctx, f"Local epoch {self.client_id}: {self.aggregation_epochs} (lr={self.lr})")
        epoch_len = len(self.train_loader[0])
        epoch = 0
        self.epoch_global = self.epoch_of_start_time + epoch
        self.log_info(fl_ctx, f"Local epoch {self.client_id}: 0 (lr={self.lr})")
        
        #self.fastai_nnet.fit(self.aggregation_epochs, 1e-5)
        self.fastai_nnet.fine_tune(self.aggregation_epochs)
        pre, target = self.fastai_nnet.get_preds(1)
        correct = 0
        _, pred_label = torch.max(pre, 1)
        correct += (pred_label == target).sum().item()
        metric = correct / len(target)
        #current_step = self.epoch_global
        current_step = epoch_len * self.epoch_global
        self.writer.add_scalar("train_loss", fastai_nnet.loss.cpu().item(), current_step)
        
        if val_freq > 0 and epoch % val_freq == 0:
            acc = self.local_valid(self.fastai_nnet, abort_signal, tb_id="val_acc_local_model", fl_ctx=fl_ctx)
            if acc > self.best_acc:
                self.save_model(is_best=True)
        
      
    def save_model(self, is_best=False):
        # save model
        model_weights = self.model.state_dict()
        print(f"======= save round {self.epoch_of_start_time} ==========")
        #model_weights = self.fastai_nnet.state_dict()
        save_dict = {"model_weights": model_weights, "epoch": self.epoch_global}
        if is_best:
            save_dict.update({"best_acc": self.best_acc})
            torch.save(save_dict, self.best_local_model_file)
        else:
            torch.save(save_dict, self.local_model_file)

    def train(self, shareable: Shareable, fl_ctx: FLContext, abort_signal: Signal) -> Shareable:
        # Check abort signal
        if abort_signal.triggered:
            return make_reply(ReturnCode.TASK_ABORTED)

        # get round information
        current_round = shareable.get_header(AppConstants.CURRENT_ROUND)
        total_rounds = shareable.get_header(AppConstants.NUM_ROUNDS)
        self.log_info(fl_ctx, f"Current/Total Round: {current_round + 1}/{total_rounds}")
        self.log_info(fl_ctx, f"Client identity: {fl_ctx.get_identity_name()}")

        # update local model weights with received weights
        dxo = from_shareable(shareable)
        global_weights = dxo.data

        # Before loading weights, tensors might need to be reshaped to support HE for secure aggregation.
        local_var_dict = self.model.state_dict()
        print("====== local_var_dict =======")
        model_keys = global_weights.keys()
        for var_name in local_var_dict:
            if var_name in model_keys:
                weights = global_weights[var_name]
                try:
                    # reshape global weights to compute difference later on
                    global_weights[var_name] = np.reshape(weights, local_var_dict[var_name].shape)
                    # update the local dict
                    local_var_dict[var_name] = torch.as_tensor(global_weights[var_name])
                except Exception as e:
                    raise ValueError("Convert weight from {} failed with error: {}".format(var_name, str(e)))
                    
        self.model.load_state_dict(local_var_dict)

        # local steps
        epoch_len = len(self.train_loader[0])
        self.log_info(fl_ctx, f"Local steps per epoch: xxx")
        # make a copy of model_global as reference for potential FedProx loss or SCAFFOLD
        model_global = copy.deepcopy(self.model)
        for param in model_global.parameters():
            param.requires_grad = False
        
        # local train
        print(f"===== local train  {self.epoch_of_start_time} =====")
        self.local_train(
            fl_ctx=fl_ctx,
            fastai_nnet=self.fastai_nnet,
            model_global=model_global,
            abort_signal=abort_signal,
            val_freq=1 if self.central else 0,
        )
        if abort_signal.triggered:
            return make_reply(ReturnCode.TASK_ABORTED)
        self.epoch_of_start_time += self.aggregation_epochs
        print(f"===== local val {self.epoch_of_start_time} =====")

        # perform valid after local train
        acc = self.local_valid(self.fastai_nnet, abort_signal, tb_id="val_acc_local_model", fl_ctx=fl_ctx)
        if abort_signal.triggered:
            return make_reply(ReturnCode.TASK_ABORTED)
        self.log_info(fl_ctx, f"val_acc_local_model: {acc:.4f}")

        # save model
        print(f"===== save model {self.epoch_of_start_time}  =====")
        self.save_model(is_best=False)
        if acc > self.best_acc:
            print("===== model is better ======")
            self.save_model(is_best=True)
        

        # compute delta model, global model has the primary key set
        print(f"===== delta model {self.epoch_of_start_time} =====")
        local_weights = self.model.state_dict()
        #local_weights = self.fastai_nnet.state_dict()
        model_diff = {}
        for name in global_weights:
            if name not in local_weights:
                continue
            model_diff[name] = local_weights[name].cpu().numpy() - global_weights[name]
            if np.any(np.isnan(model_diff[name])):
                self.system_panic(f"{name} weights became NaN...", fl_ctx)
                return make_reply(ReturnCode.EXECUTION_EXCEPTION)
        # build the shareable
        print(f"===== share model {self.epoch_of_start_time}  =====")
        dxo = DXO(data_kind=DataKind.WEIGHT_DIFF, data=model_diff)
        dxo.set_meta_prop(MetaKey.NUM_STEPS_CURRENT_ROUND, epoch_len)
        self.log_info(fl_ctx, "Local epochs finished. Returning shareable")
        return dxo.to_shareable()

    def get_model_for_validation(self, model_name: str, fl_ctx: FLContext) -> Shareable:
        # Retrieve the best local model saved during training.
        print("===== get_model_for_validation =====")
        if model_name == ModelName.BEST_MODEL:
            model_data = None
            try:
                # load model to cpu as server might or might not have a GPU
                model_data = torch.load(self.best_local_model_file, map_location="cpu")
            except Exception as e:
                self.log_error(fl_ctx, f"Unable to load best model: {e}")

            # Create DXO and shareable from model data.
            if model_data:
                dxo = DXO(data_kind=DataKind.WEIGHTS, data=model_data["model_weights"])
                return dxo.to_shareable()
            else:
                # Set return code.
                self.log_error(fl_ctx, f"best local model not found at {self.best_local_model_file}.")
                return make_reply(ReturnCode.EXECUTION_RESULT_ERROR)
        else:
            raise ValueError(f"Unknown model_type: {model_name}")  # Raised errors are caught in LearnerExecutor class.

    def local_valid(self, fastai_nnet, abort_signal: Signal, tb_id=None, fl_ctx=None):
        print("===== local_valid =====")
        self.model.eval()
        if abort_signal.triggered:
            return None
        pre, target = self.fastai_nnet.get_preds(0)
        correct = 0
        _, pred_label = torch.max(pre, 1)
        correct += (pred_label == target).sum().item()
        metric = correct / len(target)
        if tb_id:
                self.writer.add_scalar(tb_id, metric, self.epoch_global)
        return metric

    def validate(self, shareable: Shareable, fl_ctx: FLContext, abort_signal: Signal) -> Shareable:
        # Check abort signal
        if abort_signal.triggered:
            return make_reply(ReturnCode.TASK_ABORTED)
        
        #self.fastai_nnet = fastai_L(self.train_loader, self.model, loss_func=self.criterion)
        # get validation information
        self.log_info(fl_ctx, f"Client identity: {fl_ctx.get_identity_name()}")
        model_owner = shareable.get(ReservedHeaderKey.HEADERS).get(AppConstants.MODEL_OWNER)
        if model_owner:
            self.log_info(fl_ctx, f"Evaluating model from {model_owner} on {fl_ctx.get_identity_name()}")
        else:
            model_owner = "global_model"  # evaluating global model during training

        # update local model weights with received weights
        dxo = from_shareable(shareable)
        global_weights = dxo.data

        # Before loading weights, tensors might need to be reshaped to support HE for secure aggregation.
        print(f"===== validate aggregate {self.epoch_of_start_time} =====")
        local_var_dict = self.model.state_dict()
        #local_var_dict = {k: torch.as_tensor(v) for k, v in self.fastai_nnet.items()}
        #local_var_dict = self.fastai_nnet.state_dict()
        
        model_keys = global_weights.keys()
        n_loaded = 0
        for var_name in local_var_dict:
            if var_name in model_keys:
                weights = torch.as_tensor(global_weights[var_name], device=self.device)
                try:
                    # update the local dict
                    local_var_dict[var_name] = torch.as_tensor(torch.reshape(weights, local_var_dict[var_name].shape))
                    n_loaded += 1
                except Exception as e:
                    raise ValueError("Convert weight from {} failed with error: {}".format(var_name, str(e)))
        
        self.model.load_state_dict(local_var_dict)
        #self.fastai_nnet.load_state_dict(local_var_dict)
        #self.model.layers.load_state_dict(local_var_dict)
        
        if n_loaded == 0:
            raise ValueError(f"No weights loaded for validation! Received weight dict is {global_weights}")
        
        
        validate_type = shareable.get_header(AppConstants.VALIDATE_TYPE)
        if validate_type == ValidateType.BEFORE_TRAIN_VALIDATE:
            print("========= valid Before Train ==========")
            # perform valid before local train
            global_acc = self.local_valid(self.fastai_nnet, abort_signal, tb_id="val_acc_global_model", fl_ctx=fl_ctx)
            if abort_signal.triggered:
                return make_reply(ReturnCode.TASK_ABORTED)
            self.log_info(fl_ctx, f"val_acc_global_model ({model_owner}): {global_acc}")
            print(f"======= round {self.epoch_of_start_time} ==========")
            print(f"============ {global_acc} ==============")
            print(DataKind.METRICS)
            return DXO(data_kind=DataKind.METRICS, data={MetaKey.INITIAL_METRICS: global_acc}, meta={}).to_shareable()

        elif validate_type == ValidateType.MODEL_VALIDATE:
            print("========= valid After Train ==========")
            # perform valid
            train_acc = self.local_valid(self.fastai_nnet, abort_signal)
            if abort_signal.triggered:
                return make_reply(ReturnCode.TASK_ABORTED)
            self.log_info(fl_ctx, f"training acc ({model_owner}): {train_acc}")

            val_acc = self.local_valid(self.fastai_nnet, abort_signal)
            if abort_signal.triggered:
                return make_reply(ReturnCode.TASK_ABORTED)
            self.log_info(fl_ctx, f"validation acc ({model_owner}): {val_acc}")

            self.log_info(fl_ctx, "Evaluation finished. Returning shareable")

            val_results = {"train_accuracy": train_acc, "val_accuracy": val_acc}

            metric_dxo = DXO(data_kind=DataKind.METRICS, data=val_results)
            return metric_dxo.to_shareable()

        else:
            return make_reply(ReturnCode.VALIDATE_TYPE_UNKNOWN)



