import datetime
import time
import sys, os
import copy
from typing import Any

import pytorch_lightning as pl
from pytorch_lightning import LightningModule, Trainer, callbacks as cb
from pytorch_lightning.callbacks.progress.tqdm_progress import Tqdm
from torch.optim import Optimizer
from mttl.utils import Averager, logger


DEBUG = False


class MMLUCallback(cb.Callback):
    def __init__(
        self,
        eval_every_opt_step=1,
        split="test",
        max_input_length=None,
        checkpoint_oracle=True,
    ):
        super().__init__()

        self.eval_every_opt_step = eval_every_opt_step
        self.max_input_length = max_input_length
        self.evaluator = None
        self.split = split
        # save best perf
        self._best_perf = None
        # checkpointing
        self.do_checkpoint = checkpoint_oracle
        self._checkpoint_now = False
        self._prev_checkpoint = None
        # debug
        self.eval_mmlu_count = 0

    @property
    def last_chkpt(self):
        return self._prev_checkpoint

    @property
    def best_perf(self):
        return self._best_perf

    @best_perf.setter
    def best_perf(self, value):
        if self._best_perf is None:
            self._best_perf = value
            self._checkpoint_now = True
        else:
            if value["all"]["mean"] > self._best_perf["all"]["mean"]:
                self._checkpoint_now = True
            for (k1, v1), (k2, v2) in zip(self._best_perf.items(), value.items()):
                if v2["mean"] > v1["mean"]:
                    self._best_perf[k1] = v2

    def on_before_optimizer_step(
        self, trainer: Trainer, pl_module: LightningModule, optimizer: Optimizer
    ) -> None:
        if trainer.global_step % self.eval_every_opt_step == 0:
            metrics = self.eval_mmlu(pl_module)
            self.best_perf = copy.deepcopy(metrics)
            self.maybe_checkpoint_now(trainer)
            self.log_metrics(metrics, pl_module)

        return super().on_before_optimizer_step(trainer, pl_module, optimizer)

    def maybe_checkpoint_now(self, trainer):
        if self.do_checkpoint and self._checkpoint_now:
            dir_name = trainer.checkpoint_callback.dirpath
            filename = (
                trainer.checkpoint_callback.filename.split("{")[0]
                + f"mmlu_{self.split}_oracle/"
                + f"{self.best_perf['all']['mean']:.004f}.ckpt"
            )
            ckpt_path = os.path.join(dir_name, filename)
            trainer.save_checkpoint(ckpt_path)
            # if self._prev_checkpoint is not None and ckpt_path != self._prev_checkpoint:
            #     os.remove(self._prev_checkpoint)
            self._prev_checkpoint = ckpt_path
        self._checkpoint_now = False

    def on_validation_epoch_end(
        self, trainer: Trainer, pl_module: LightningModule
    ) -> None:
        # log best perf
        if self.best_perf is not None:
            for t, v in self.best_perf.items():
                pl_module.log(
                    f"downstream_best/{self.split}/oracle/mmlu_{t}",
                    v["mean"],
                    on_epoch=True,
                )
        return super().on_validation_epoch_end(trainer, pl_module)

    def log_metrics(self, metrics, pl_module: pl.LightningModule, on_step=True):
        for t, v in metrics.items():
            pl_module.log(
                f"downstream/{self.split}/mmlu_{t}",
                v["mean"],
                on_step=on_step,
            )

    def eval_mmlu(self, pl_module):
        from mttl.evaluators import MMLUEvaluator
        from mttl.datamodule.mmlu_data_module import MMLUDataConfig

        if DEBUG:
            self.eval_mmlu_count += 1
            return {"all": {"mean": self.eval_mmlu_count}}

        if self.evaluator is None:
            mmlu_data_config = MMLUDataConfig(
                model=pl_module.hparams.model,
                predict_batch_size=pl_module.hparams.predict_batch_size,
                max_input_length=pl_module.hparams.max_input_length
                if self.max_input_length is None
                else self.max_input_length,
                max_output_length=pl_module.hparams.max_input_length,  # not necessary
                model_family=pl_module.hparams.model_family,
                finetune_task_name=pl_module.hparams.finetune_task_name,
            )
            self.evaluator = MMLUEvaluator(
                mmlu_data_config,
                split=self.split,
            )

        metrics = self.evaluator.evaluate(pl_module)
        return metrics

    def on_after_backward(self, *args, **kwargs):
        self.val_epoch += 1

        trainer, pl_module = args

        if trainer.global_step < 1:
            return

        if self.val_epoch % self.every_val_epochs != 0:
            return

        from mttl.evaluators import MMLUEvaluator

        evaluator = MMLUEvaluator(
            pl_module.hparams,
            **self.eval_kwargs,
        )
        metrics = evaluator.evaluate(
            pl_module,
            subsample=10,
        )
        pl_module.log(
            "val/mmlu",
            metrics["all"]["mean"],
            on_step=False,
            on_epoch=True,
            prog_bar=True,
        )


class NICallback(cb.Callback):
    def __init__(self, every_val_epochs=1, **kwargs):
        super().__init__()

        self.val_epoch = 0
        self.every_val_epochs = every_val_epochs
        self.eval_kwargs = kwargs

    def on_validation_epoch_end(self, trainer, pl_module) -> None:
        self.val_epoch += 1

        if trainer.global_step == 0:
            return

        if self.val_epoch % self.every_val_epochs != 0:
            return

        from mttl.evaluators import NIEvaluator

        evaluator = NIEvaluator(
            pl_module.hparams,
            num_pos_examples=2,
            **self.eval_kwargs,
        )
        metrics = evaluator.evaluate(
            pl_module,
            eval_batches=50,
        )
        pl_module.log(
            "val/sni",
            metrics["all"]["mean"],
            on_step=False,
            on_epoch=True,
            prog_bar=True,
        )


class MiniProgress(cb.ProgressBar):
    def __init__(self):
        super().__init__()
        self.averager = Averager(0.9)

    def on_train_batch_start(
        self, trainer, pl_module, batch: Any, batch_idx: int
    ) -> None:
        self.time_start = time.time()

    def on_train_batch_end(
        self,
        trainer,
        pl_module,
        outputs,
        batch,
        batch_idx,
    ) -> None:
        self.time_end = time.time()
        metrics = self.get_metrics(trainer, pl_module)
        metrics = {k: v for k, v in metrics.items()}
        it_per_sec = 1 / (self.time_end - self.time_start)

        # num total steps will be min of num_training_batches and max_steps
        if trainer.max_steps > -1:
            num_total_steps = min(
                trainer.num_training_batches * trainer.max_epochs, trainer.max_steps
            )
        else:
            num_total_steps = (
                trainer.num_training_batches // trainer.accumulate_grad_batches
            ) * trainer.max_epochs

        eta = (num_total_steps - batch_idx) / (
            1.0 / ((self.time_end - self.time_start))
        )

        time_metrics = self.averager.update({"it/s": it_per_sec, "eta": eta})
        for k, v in {**metrics, **time_metrics}.items():
            if k == "eta":
                metrics[k] = "{}".format(datetime.timedelta(seconds=v))
            else:
                metrics[k] = "{:.2f}".format(v) if isinstance(v, float) else v

        msg_start = (
            f"Trn - Epc {trainer.current_epoch} / {trainer.global_step} / {num_total_steps}"
            + " | "
        )
        dict_msg = " | ".join([f"{k} -> {v}" for k, v in metrics.items()]) + " | "
        msg = msg_start + dict_msg
        logger.info(msg)

    def on_validation_batch_start(
        self, trainer, pl_module, batch: Any, batch_idx: int
    ) -> None:
        self.time_start = time.time()

    def on_validation_batch_end(
        self,
        trainer,
        pl_module,
        outputs,
        batch,
        batch_idx,
    ) -> None:
        self.time_end = time.time()
        metrics = self.get_metrics(trainer, pl_module)
        metrics = {k: v for k, v in metrics.items()}
        metrics["it/s"] = 1.0 / (self.time_end - self.time_start)
        for k, v in metrics.items():
            metrics[k] = "{:.2f}".format(v) if isinstance(v, float) else v

        msg_start = (
            f"Val - Epc {trainer.current_epoch} / {batch_idx} / {trainer.num_val_batches[0]}"
            + " | "
        )
        dict_msg = " | ".join([f"{k} -> {v}" for k, v in metrics.items()]) + " | "
        msg = msg_start + dict_msg
        logger.info(msg)


class ProgressCallback(cb.TQDMProgressBar):
    def init_sanity_tqdm(self) -> Tqdm:
        """Override this to customize the tqdm bar for the validation sanity run."""
        bar = Tqdm(
            desc="Validation sanity check",
            position=(2 * self.process_position),
            disable=self.is_disabled,
            leave=False,
            dynamic_ncols=True,
            file=sys.stderr,
        )
        return bar

    def init_train_tqdm(self) -> Tqdm:
        """Override this to customize the tqdm bar for training."""
        bar = Tqdm(
            desc="Training",
            position=(2 * self.process_position),
            disable=self.is_disabled,
            leave=True,
            dynamic_ncols=True,
            file=sys.stderr,
            smoothing=0,
        )
        return bar

    def init_predict_tqdm(self) -> Tqdm:
        """Override this to customize the tqdm bar for predicting."""
        bar = Tqdm(
            desc="Predicting",
            position=(2 * self.process_position),
            disable=self.is_disabled,
            leave=True,
            dynamic_ncols=True,
            file=sys.stderr,
            smoothing=0,
        )
        return bar

    def init_validation_tqdm(self) -> Tqdm:
        """Override this to customize the tqdm bar for validation."""
        # The main progress bar doesn't exist in `trainer.validate()`
        has_main_bar = self.trainer.state.fn != "validate"
        bar = Tqdm(
            desc="Validating",
            position=(2 * self.process_position + has_main_bar),
            disable=self.is_disabled,
            leave=False,
            dynamic_ncols=True,
            file=sys.stderr,
        )
        return bar

    def init_test_tqdm(self) -> Tqdm:
        """Override this to customize the tqdm bar for testing."""
        bar = Tqdm(
            desc="Testing",
            position=(2 * self.process_position),
            disable=self.is_disabled,
            leave=True,
            dynamic_ncols=True,
            file=sys.stderr,
        )
        return bar
