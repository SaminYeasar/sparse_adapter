import os
import sys
import torch
import copy
import wandb
import numpy as np
from copy import deepcopy
import torch.nn.functional as F
from pytorch_lightning import seed_everything
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

from mttl.models.library.expert_library import ExpertLibrary
from mttl.models.containers.selectors.base import Selector
from mttl.models.modifiers.lora import LoRAConfig

from mttl.utils import logger, remote_login, setup_logging
from mttl.models.expert_model import MultiExpertModel, ExpertModel
from mttl.models.expert_config import ExpertConfig

from mttl.evaluators.base import EvaluatorRunner, setup_evaluators
from mttl.models.lightning.callbacks import LossCallback
from mttl.datamodule.base import get_datamodule
from mttl.evaluators.rouge_evaluator import RougeEvaluator
from mttl.logging import TableLogger


def eval_in_distribution(module, args: ExpertConfig, tasks: list):
    args.include_task_source = "*"
    transfer_table = TableLogger()
    print(f"eval metric: {args.eval_metric}")

    for i, task in enumerate(tasks):
        args.finetune_task_name = task
        args.predict_batch_size = 16
        if args.eval_metric in ["val_loss", "loss"]:
            dm = get_datamodule(args)
            evaluator = LossCallback(
                dm.val_dataloader(), output_dir=args.output_dir, name=task + "_val"
            )
            metric = evaluator.test(pl_module=module).item()

        elif args.eval_metric == "test_loss":
            dm = get_datamodule(args)
            evaluator = LossCallback(
                dm.test_dataloader(), output_dir=args.output_dir, name=task + "_test"
            )
            metric = evaluator.test(pl_module=module).item()
        elif args.eval_metric == "val_rougeL":
            dm = get_datamodule(args, for_generation=True)
            evaluator = RougeEvaluator(
                datamodule=dm,
            )
            metric = evaluator.evaluate(
                module,
                split="val",
                verbose=False,
            )
        elif args.eval_metric == "rougeL":
            dm = get_datamodule(args, for_generation=True)
            evaluator = RougeEvaluator(
                datamodule=dm,
            )
            metric = evaluator.evaluate(
                module,
                split="test",
                verbose=False,
            )
        else:
            raise ValueError(f"Unknown eval metric {args.eval_metric}")
        if wandb.run is not None:
            wandb.log({f"test/{args.eval_metric}_{task}": metric})
        transfer_table.log({"task": task, args.eval_metric: metric})

    if wandb.run is not None:
        wandb.log(
            {f"mean_{args.eval_metric}": transfer_table.df[args.eval_metric].mean()}
        )

    transfer_table.log(
        {
            "task": "mean",
            args.eval_metric: transfer_table.df[args.eval_metric].mean(),
        }
    )
    transfer_table.log_final_table()


def eval_in_distribution_sparse_model(
    module, library, expert, args: ExpertConfig, tasks: list
):
    args.include_task_source = "*"
    transfer_table = TableLogger()

    for i, task in enumerate(tasks):
        # update the mask correspond to the task
        expert.update_module_mask(module, library[task])

        args.finetune_task_name = task
        args.predict_batch_size = 16
        if args.eval_metric in ["val_loss", "loss"]:
            dm = get_datamodule(args)
            evaluator = LossCallback(
                dm.val_dataloader(), output_dir=args.output_dir, name=task + "_val"
            )
            metric = evaluator.test(pl_module=module).item()

        elif args.eval_metric == "test_loss":
            dm = get_datamodule(args)
            evaluator = LossCallback(
                dm.test_dataloader(), output_dir=args.output_dir, name=task + "_test"
            )
            metric = evaluator.test(pl_module=module).item()
        elif args.eval_metric == "val_rougeL":
            dm = get_datamodule(args, for_generation=True)
            evaluator = RougeEvaluator(
                datamodule=dm,
            )
            metric = evaluator.evaluate(
                module,
                split="val",
                verbose=False,
            )
        elif args.eval_metric == "rougeL":
            dm = get_datamodule(args, for_generation=True)
            evaluator = RougeEvaluator(
                datamodule=dm,
            )
            metric = evaluator.evaluate(
                module,
                split="test",
                verbose=False,
            )
        else:
            raise ValueError(f"Unknown eval metric {args.eval_metric}")
        if wandb.run is not None:
            wandb.log({f"test/{args.eval_metric}_{task}": metric})
        transfer_table.log({"task": task, args.eval_metric: metric})

    if wandb.run is not None:
        wandb.log(
            {f"mean_{args.eval_metric}": transfer_table.df[args.eval_metric].mean()}
        )

    transfer_table.log(
        {
            "task": "mean",
            args.eval_metric: transfer_table.df[args.eval_metric].mean(),
        }
    )
    transfer_table.log_final_table()


def run_eval(args: ExpertConfig):
    seed_everything(args.seed, workers=True)

    # get directory of the current file
    setup_logging(args.output_dir)

    logger.info("Args: {}".format(args.to_json()))

    remote_login(args.remote_token)

    # defult
    selection = None  # for debugging: selection = ['duorc_ParaphraseRC_extract_answer', 'wiki_qa_Topic_Prediction_Question_and_Answer_Pair']
    if selection is None:
        exclude_phi_tasks = [
            "hellaswag_1_1_0",
            "ai2_arc_ARC_Challenge_1_0_0",
            "ai2_arc_ARC_Easy_1_0_0",
            "piqa_1_0_0",
            "winogrande_1_1_0",
            "bool_q_1_0_0",
            "openbookqa_0_1_0",
        ]
    else:
        exclude_phi_tasks = None
    print(args.library_id)
    library = ExpertLibrary.get_expert_library(
        repo_id=args.library_id,
        token=args.remote_token,
        exclude_selection=exclude_phi_tasks,
        destination_id=args.destination_library_id,
        selection=selection,
        N_experts=args.N_experts,
    )
    an_expert = library[next(iter(library.keys()))]
    train_cfg = deepcopy(an_expert.training_config)
    train_cfg.device_map = "cpu"
    # For starts, always overwrite the following arguments
    for arg_name in [
        "output_dir",
        "eval_metric",
        "remove_phi_eval_tasks",
        "include_task_source",
    ]:
        value = getattr(args, arg_name, None)
        setattr(train_cfg, arg_name, value)

    """ Parameter Merging Approaches """
    if args.merge_or_route == "uniform":
        from mttl.models.library.merging_methods.uniform_merge import (
            UniformMerge,
            UniformMergeConfig,
        )

        cfg = UniformMergeConfig(alpha=args.merge_alpha)
        module = UniformMerge(cfg).transform(library).to("cuda")

    elif args.merge_or_route == "ties":
        from mttl.models.library.merging_methods.ties import (
            TiesMergeSimple,
            TiesMergeSimpleConfig,
        )

        cfg = TiesMergeSimpleConfig(alpha=args.merge_alpha)
        module = TiesMergeSimple(cfg).transform(library).to("cuda")

    elif args.merge_or_route == "model_breadcrumbs":
        from mttl.models.library.merging_methods.model_breadcrumbs import (
            ModelBreadcrumbs,
            ModelBreadcrumbsConfig,
        )

        cfg = ModelBreadcrumbsConfig(alpha=args.merge_alpha)
        module = ModelBreadcrumbs(cfg).transform(library).to("cuda")

    elif args.merge_or_route == "task_arithmetic":
        from mttl.models.library.merging_methods.task_arithmetic import (
            TaskArithmetic,
            TaskArithmeticConfig,
        )

        cfg = TaskArithmeticConfig(alpha=args.merge_alpha)
        module = TaskArithmetic(cfg).transform(library).to("cuda")

    elif args.merge_or_route == "SLERP":
        from mttl.models.library.merging_methods.slerp import (
            SLERPMerge,
            SLERPMergeConfig,
        )

        module = SLERPMerge(SLERPMergeConfig()).transform(library).to("cuda")

    elif args.merge_or_route == "uniform_lora_before_op":
        from mttl.models.library.merging_methods.LoRA_ablinear import (
            LoRA_ab_LinearMerge,
            LoRA_ab_LinearMergeConfig,
        )

        module = (
            LoRA_ab_LinearMerge(LoRA_ab_LinearMergeConfig())
            .transform(library)
            .to("cuda")
        )

    elif args.merge_or_route in [
        "uniform_sparse_weight",
        "uniform_sparse_weight_oracle_routing",
    ]:
        """uniform merge of all weights"""
        from mttl.models.library.merging_methods.uniform_sparse import (
            UniformSparse,
            UniformSparsConfig,
        )

        expert = UniformSparse(UniformSparsConfig())
        module = expert.transform(library).to("cuda")

    elif args.merge_or_route == "uniform_lora_after_op":
        # Here we merge the LoRA experts after the outer product we cannot really do it
        # with the lib transform, cause this would require storing large matrices in memory
        # Instead we do it with a uniform selector
        assert type(an_expert.expert_info.expert_config) == LoRAConfig
        train_cfg.router_selector = "uniform"
        train_cfg.lora_merge_after = True
        module = MultiExpertModel(**vars(train_cfg)).to("cuda")
        module.load_from_module_dict(library)

    elif args.merge_or_route == "base":
        module = ExpertModel(**vars(train_cfg)).to("cuda")

    else:
        raise ValueError(f"Unknown merge_or_route {args.merge_or_route}")

    metric_logger = Selector.metric_logger

    if wandb.run is None and os.environ.get("WANDB_API_KEY"):
        wandb.init(
            project=os.environ.get("WANDB_PROJECT", "0shot_routing"),
            config=dict(module.hparams),
            name=os.environ.get("AMLT_JOB_NAME", None),
        )
        # update config
        wandb.config.update({f"cmd_args_{k}": v for k, v in vars(args).items()})

    if args.pipeline_eval_tasks in [
        "in_distribution",
    ]:
        tasks = [expert.expert_task_name for expert in library.data.values()]
        if tasks[0] is None:
            # for some older version of lib (in case of joint experts) no expert_task_name was set
            tasks = json.load(open(args.flan_tasks_path))["flan256"]
        # make sure we evaluate each task seperately (so the mean is over tasks at the end)
        tasks = ",".join(tasks).split(",")
        train_cfg.eval_metric = args.eval_metric
        train_cfg.subsample_dev = args.subsample_dev

        # debug with in task: tasks = [expert_names[0]]
        if args.merge_or_route == "uniform_sparse_weight_oracle_routing":
            scores = eval_in_distribution_sparse_model(
                module, library, expert, train_cfg, tasks
            )
        else:
            scores = eval_in_distribution(module, train_cfg, tasks)

    elif args.pipeline_eval_tasks in [
        "out_distribution",
    ]:
        # give eval tasks in `finetune_task_name` argument
        if isinstance(args.finetune_task_name, tuple):
            tasks = list(args.finetune_task_name)
        elif isinstance(args.finetune_task_name, str):
            tasks = args.finetune_task_name.split(",")

        train_cfg.eval_metric = args.eval_metric
        train_cfg.subsample_dev = args.subsample_dev
        scores = eval_in_distribution(module, train_cfg, tasks)

    else:
        if args.pipeline_eval_tasks == "all":
            args.pipeline_eval_tasks = "arc-challenge,arc-easy,boolq,hellaswag,humaneval,mbpp,openbookqa,piqa,bbh-fast,winogrande"

        with torch.no_grad():
            runner: EvaluatorRunner = setup_evaluators(
                model_type=module.hparams.model,
                model_family=module.hparams.model_family,
                max_input_length=module.hparams.max_input_length,
                max_output_length=module.hparams.max_output_length,
                predict_batch_size=args.predict_batch_size,
                truncation_side=module.hparams.truncation_side,
                tasks=args.pipeline_eval_tasks,
                output_path=os.path.join(args.output_dir, "DOWNSTREAM"),
                add_eos_to_targets=args.add_eos_to_downstream_targets,
            )
            scores = runner.run(module)

    if len(metric_logger) > 0:
        task_table = metric_logger.pretty_table(match_on="task|.*uniform.*")
        layer_table = metric_logger.pretty_table(match_on="layer|.*uniform.*")
        expert_p = metric_logger.pretty_table(match_on=".*expert_p|.*uniform.*")
        angle = metric_logger.pretty_table(match_on=".*angle.*")
        print(task_table)
        print(layer_table)
        print(expert_p)
        print(angle)

    if wandb.run is not None:
        if scores is not None:
            wandb.log({f"downstream/{k}": v for k, v in scores.items()})
        if len(metric_logger) > 0:
            wandb.log({k: v.avg for k, v in metric_logger.meters.items()})

        wandb.finish()


if __name__ == "__main__":
    args = ExpertConfig.parse()
    run_eval(args)
