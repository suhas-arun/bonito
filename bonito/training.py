"""
Bonito train
"""

import math
import os
import re
from glob import glob
from itertools import islice
from time import perf_counter
from collections import OrderedDict
from datetime import datetime

from bonito.schedule import linear_warmup_cosine_decay
from bonito.util import accuracy, decode_ref, permute, match_names, tqdm_environ, load_object
import bonito

import torch
import numpy as np
from tqdm import tqdm
import torch.cuda.amp as amp


def load_state(dirname, device, model, optim=None):
    """
    Load a model state dict from disk
    """
    model.to(device)
    if hasattr(model, "module"):
        model = model.module

    weight_no = optim_no = None

    optim_files = glob(os.path.join(dirname, "optim_*.tar"))
    optim_nos = {int(re.sub(".*_([0-9]+).tar", "\\1", w)) for w in optim_files}

    weight_files = glob(os.path.join(dirname, "weights_*.tar"))
    weight_nos = {int(re.sub(".*_([0-9]+).tar", "\\1", w)) for w in weight_files}

    if optim is not None:
        weight_no = optim_no = max(optim_nos & weight_nos, default=None)
    else:
        weight_no = max(weight_nos, default=None)

    to_load = []
    if weight_no:
        to_load.append(("weights", model))
    if optim_no:
        to_load.append(("optim", optim))

    if to_load:
        print("[picking up %s state from epoch %s]" % (', '.join([n for n, _ in to_load]), weight_no))
        for name, obj in to_load:
            state_dict = torch.load(
                os.path.join(dirname, '%s_%s.tar' % (name, weight_no)), map_location=device
            )
            if name == "weights":
                state_dict = {k2: state_dict[k1] for k1, k2 in match_names(state_dict, obj).items()}
                new_state_dict = OrderedDict()
                for k, v in state_dict.items():
                    name = k.replace('module.', '')
                    new_state_dict[name] = v
                state_dict = new_state_dict
            obj.load_state_dict(state_dict)
        epoch = weight_no
    else:
        epoch = 0

    return epoch


class ClipGrad:
    def __init__(self, quantile=0.5, factor=2.0, buffer_size=100):
        self.buffer = np.full(buffer_size, fill_value=1e6)
        self.quantile = quantile
        self.factor = factor
        self.i = 0

    def append(self, grad_norm):
        self.buffer[self.i] = grad_norm
        self.i = (self.i + 1) % len(self.buffer)

    def __call__(self, parameters):
        max_norm = self.factor * np.quantile(self.buffer, self.quantile)
        grad_norm = torch.nn.utils.clip_grad_norm_(parameters, max_norm=max_norm).item()
        if not math.isnan(grad_norm):
            self.append(grad_norm)
        return grad_norm


class Trainer:
    def __init__(
        self, model, device, train_loader, valid_loader, criterion=None,
        use_amp=True, lr_scheduler_fn=None, restore_optim=False,
        save_optim_every=10, grad_accum_split=1, quantile_grad_clip=False,
        chunks_per_epoch=None, batch_size=None, pre_training=False
    ):
        self.model = model.to(device)
        self.device = device
        self.train_loader = train_loader
        self.valid_loader = valid_loader
        self.criterion = criterion or model.loss
        self.use_amp = use_amp
        self.lr_scheduler_fn = lr_scheduler_fn or linear_warmup_cosine_decay()
        self.restore_optim = restore_optim
        self.save_optim_every = save_optim_every
        self.grad_accum_split = grad_accum_split
        self.scaler = torch.cuda.amp.GradScaler(enabled=use_amp)
        self.optimizer = None
        if quantile_grad_clip:
            self.clip_grad = ClipGrad()
        else:
            self.clip_grad = lambda parameters: torch.nn.utils.clip_grad_norm_(parameters, max_norm=2.0).item()

        self.batch_size = batch_size
        self.chunks_per_epoch = chunks_per_epoch
        self.steps_per_epoch = chunks_per_epoch // batch_size
        self.pre_training = pre_training

    def train_one_step(self, batch):
        self.optimizer.zero_grad()

        losses = None
        with amp.autocast(enabled=self.use_amp):
            for batch_ in zip(
                *map(lambda t: t.chunk(self.grad_accum_split, dim=0), batch)
            ):
                if self.pre_training:
                    data_, hp_lengths, is_hp, hp_bases = (x.to(self.device) for x in batch_)
                    hp_labels = {
                        'hp_lengths': hp_lengths,
                        'is_hp': is_hp,
                        'hp_bases': hp_bases
                    }

                    targets_, lengths_ = None, None
                    scores_ = self.model(data_, hp_true_labels=hp_labels)
                else:
                    data_, targets_, lengths_, *args = (x.to(self.device) for x in batch_)
                    scores_ = self.model(data_, *args)
                
                losses_ = self.criterion(scores_, targets_, lengths_)

                if not isinstance(losses_, dict): losses_ = {'loss': losses_}

                total_loss = losses_.get('total_loss', losses_['loss']) / self.grad_accum_split
                self.scaler.scale(total_loss).backward()

                losses = {
                    k: ((v.item() / self.grad_accum_split) if losses is None else (v.item() / self.grad_accum_split) + losses[k])
                    for k, v in losses_.items()
                }

        scale = self.scaler.get_scale()
        self.scaler.unscale_(self.optimizer)
        grad_norm = self.clip_grad(self.model.parameters())
        self.scaler.step(self.optimizer)
        self.scaler.update()

        return losses, grad_norm, scale

    def train_one_epoch(self, loss_log, lr_scheduler):
        t0 = perf_counter()
        chunks = 0
        self.model.train()

        # total is in batches and desc represents the number of training chunks supplied
        progress_bar = tqdm(
            total=self.steps_per_epoch, desc='[0/{}]'.format(self.chunks_per_epoch),
            ascii=True, leave=True, ncols=100, bar_format='{l_bar}{bar}| [{elapsed}{postfix}]',
            **tqdm_environ()
        )
        smoothed_losses = {}

        with progress_bar:

            for batch in islice(self.train_loader, self.steps_per_epoch):
                chunks += batch[0].shape[0]
                losses, grad_norm, scale = self.train_one_step(batch)

                for k, v in losses.items():
                    if k not in smoothed_losses:
                        smoothed_losses[k] = v
                    else:
                        smoothed_losses[k] = 0.01 * v + 0.99 * smoothed_losses[k]

                if self.pre_training:
                    postfix_dict = {
                        k.rstrip("_loss"): f"{v:.4f}"
                        for k, v in smoothed_losses.items()
                        if k != "ctc_loss"
                    } 
                    progress_bar.set_postfix(**postfix_dict)
                else:
                    main_loss = smoothed_losses.get('loss', 0.0)
                    progress_bar.set_postfix(loss='%.4f' % main_loss)

                progress_bar.set_description("[{}/{}]".format(chunks, self.chunks_per_epoch))
                progress_bar.update()

                if loss_log is not None:
                    lr = lr_scheduler.get_last_lr()
                    if len(lr) == 1: lr = lr[0]
                    loss_log.append({
                        'chunks': chunks,
                        'time': perf_counter() - t0,
                        'grad_norm': grad_norm,
                        'lr': lr,
                        'scale': scale,
                        **losses
                    })

                if lr_scheduler is not None: lr_scheduler.step()

        smoothed_loss = smoothed_losses.get('loss', 0.0)
        return smoothed_loss, perf_counter() - t0

    def validate_one_step(self, batch):
        with amp.autocast(enabled=self.use_amp):
            if self.pre_training:
                data, hp_lengths, is_hp, hp_bases = (x.to(self.device) for x in batch)
                hp_labels = {
                    'hp_lengths': hp_lengths,
                    'is_hp': is_hp,
                    'hp_bases': hp_bases
                }
                scores = self.model(data, hp_true_labels=hp_labels)
                losses = self.criterion(scores, None, None)
            else:
                data, targets, lengths, *args = batch
                scores = self.model(data.to(self.device), *(x.to(self.device) for x in args))
                losses = self.criterion(scores, targets.to(self.device), lengths.to(self.device))

        losses = {k: v.item() for k, v in losses.items()} if isinstance(losses, dict) else losses.item()
        if self.pre_training:
            return [], [], [], losses

        if isinstance(scores, dict):
                scores = scores['logits']
        if hasattr(self.model, 'decode_batch'):
            seqs = self.model.decode_batch(scores)
        else:
            seqs = [self.model.decode(x) for x in permute(scores, 'TNC', 'NTC')]
        refs = [decode_ref(target, self.model.alphabet) for target in targets]

        n_pre = getattr(self.model, "n_pre_context_bases", 0)
        n_post = getattr(self.model, "n_post_context_bases", 0)
        if n_pre > 0 or n_post > 0:
            refs = [ref[n_pre:len(ref)-n_post] for ref in refs]

        accs = [
            accuracy(ref, seq, min_coverage=0.5) if len(seq) else 0. for ref, seq in zip(refs, seqs)
        ]
        return seqs, refs, accs, losses

    def validate_one_epoch(self):
        self.model.eval()
        with torch.no_grad():
            seqs, refs, accs, losses = zip(*(self.validate_one_step(batch) for batch in self.valid_loader))
        loss = np.mean([(x['loss'] if isinstance(x, dict) else x) for x in losses])
        if self.pre_training:
            return loss, 0.0, 0.0
        seqs, refs, accs = (sum(x, []) for x in (seqs, refs, accs))
        return loss, np.mean(accs), np.median(accs)

    def init_optimizer(self, lr, **optim_kwargs):
        if "package" in optim_kwargs:
            optim_cls = load_object(optim_kwargs.pop('package'), optim_kwargs.pop('symbol'))
        else:
            optim_cls = torch.optim.AdamW

        print(f"[loading optim] - '{optim_cls.__name__}' with args: {optim_kwargs}")
        optim_kwargs["lr"] = lr
        self.optimizer = optim_cls(self.model.parameters(), **optim_kwargs)


    def get_lr_scheduler(self, epochs, last_epoch=0):
        return self.lr_scheduler_fn(self.optimizer, self.steps_per_epoch, epochs, last_epoch)

    def fit(self, workdir, epochs=1, lr=2e-3, **optim_kwargs):
        if self.optimizer is None:
            self.init_optimizer(lr, **optim_kwargs)

        last_epoch = load_state(workdir, self.device, self.model, self.optimizer if self.restore_optim else None)

        if self.restore_optim:
        # override learning rate to new value
            for i, pg in enumerate(self.optimizer.param_groups):
                pg["initial_lr"] = pg["lr"] = lr[i] if isinstance(lr, (list, tuple)) else lr

        lr_scheduler = self.get_lr_scheduler(epochs, last_epoch=last_epoch)

        for epoch in range(1 + last_epoch, epochs + 1):
            try:
                with bonito.io.CSVLogger(os.path.join(workdir, 'losses_{}.csv'.format(epoch))) as loss_log:
                    train_loss, duration = self.train_one_epoch(loss_log, lr_scheduler)

                model_state = self.model.module.state_dict() if hasattr(self.model, 'module') else self.model.state_dict()
                torch.save(model_state, os.path.join(workdir, "weights_%s.tar" % epoch))
                if epoch % self.save_optim_every == 0:
                    torch.save(self.optimizer.state_dict(), os.path.join(workdir, "optim_%s.tar" % epoch))

                val_loss, val_mean, val_median = self.validate_one_epoch()
            except KeyboardInterrupt:
                break

            print("[epoch {}] directory={} loss={:.4f} mean_acc={:.3f}% median_acc={:.3f}%".format(
                epoch, workdir, val_loss, val_mean, val_median
            ))

            with bonito.io.CSVLogger(os.path.join(workdir, 'training.csv')) as training_log:
                if self.pre_training:
                    training_log.append({
                        'time': datetime.today(),
                        'duration': int(duration),
                        'epoch': epoch,
                        'train_loss': train_loss
                    })
                else:
                    training_log.append({
                        'time': datetime.today(),
                        'duration': int(duration),
                        'epoch': epoch,
                        'train_loss': train_loss,
                        'validation_loss': val_loss,
                        'validation_mean': val_mean,
                        'validation_median': val_median
                    })
