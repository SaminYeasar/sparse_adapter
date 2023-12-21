from functools import partial
import os
import numpy
from datasets import load_dataset, concatenate_datasets
from datasets import Dataset
from mttl.datamodule.base import DefaultDataModule, DatasetConfig
from mttl.datamodule.utils import maybe_filter_hf_dataset_by_task, logger
from dataclasses import dataclass
import tqdm


def augment_few_shot_task(
    dataset, num_samples, tokenizer=None, max_input_length=None, seed=42
):
    len_dataset = len(dataset)
    split = dataset["split"]
    rng = numpy.random.RandomState(seed)
    augmented_dataset = []

    train_indices = set(i for i in range(len_dataset) if split[i] == "train")

    def map_to_few_shot(example, index):
        index_range = list(train_indices - {index})
        index_chosen = rng.choice(index_range, size=num_samples, replace=False)
        index_chosen = list(map(int, index_chosen))  # datasets complains otherwise

        sources = [dataset[i]["source"] for i in index_chosen]
        targets = [dataset[i]["target"] for i in index_chosen]

        context = (
            "\n\n".join(
                [" ".join([source, target]) for source, target in zip(sources, targets)]
            )
            + "\n\n"
        )
        prompt = context + dataset[index]["source"]

        if tokenizer is not None and max_input_length is not None:
            input_ids = tokenizer(prompt, return_tensors="pt").input_ids

            while (
                input_ids.shape[-1] > max_input_length
                and len(context.split("\n\n")) > 2
            ):
                context = "\n\n".join(context.split("\n\n")[:-2]) + "\n\n"
                prompt = context + dataset[index]["source"]
                input_ids = tokenizer(prompt, return_tensors="pt").input_ids

        return {
            "source": prompt,
            "target": dataset[index]["target"],
            "task_name": dataset[index]["task_name"],
            "task_source": "few_shot_{}".format(dataset[index]["task_source"]),
            "split": dataset[index]["split"],
        }

    augmented_dataset = dataset.map(map_to_few_shot, with_indices=True, num_proc=16)
    return augmented_dataset


def augment_few_shot(
    dataset, num_samples, tokenizer=None, max_input_length=None, seed=42
):
    """Augment the dataset with few-shot examples."""
    import numpy as np
    import tqdm

    augmented_dataset = []
    for source in tqdm.tqdm(dataset.unique("task_name")):
        augmented_dataset.append(
            Dataset.from_list(
                augment_few_shot_task(
                    dataset.filter(lambda x: x["task_name"] == source),
                    num_samples,
                    tokenizer,
                    max_input_length,
                    seed,
                )
            )
        )
    return concatenate_datasets([dataset, augmented_dataset])


@dataclass
class FlatMultiTaskConfig(DatasetConfig):
    source_template: str = None
    augment_few_shot: int = 0


def apply_source_template(source_template, example):
    example["source"] = source_template.format(example["source"])
    return example


class FlatMultiTaskModule(DefaultDataModule):
    def setup_dataset(self):
        self.dataset = load_dataset(self.config.dataset)
        n_proc = int(os.environ.get("MTTL_NUM_PROC_DATASETS", 16))
        if "split" not in self.dataset.column_names["train"]:
            raise ValueError(
                "Dataset must have a 'split' column, try removing the dataset manually from the cache."
            )
        (
            self._task_names,
            self._task_to_id,
            train_dataset,
            _,
            _,
        ) = maybe_filter_hf_dataset_by_task(
            self.dataset, "task_name", self.config.finetune_task_name, n_proc=n_proc
        )

        if self.config.source_template is not None:
            # apply source template if specified
            train_dataset = train_dataset.map(
                partial(apply_source_template, self.config.source_template),
                num_proc=n_proc,
            )

        if self.config.augment_few_shot > 0:
            train_dataset_aug = augment_few_shot(
                train_dataset,
                self.config.augment_few_shot,
                tokenizer=self.tokenizer,
                max_input_length=self.config.max_input_length,
            )
            train_dataset_aug = train_dataset_aug.shuffle()
            train_dataset = train_dataset_aug.select(range(len(train_dataset)))

        self.train_dataset = train_dataset.filter(
            lambda x: x["split"] == "train",
            num_proc=n_proc,
            desc="Creating train set",
        )
        self.dev_dataset = train_dataset.filter(
            lambda x: x["split"] in ["validation", "valid"],
            num_proc=n_proc,
            desc="Creating valid set",
        )
        self.test_dataset = train_dataset.filter(
            lambda x: x["split"] == "test",
            num_proc=n_proc,
            desc="Creating test set",
        )

        if len(self.test_dataset) == 0:
            self.test_dataset = self.dev_dataset

        self.print_infos()


@dataclass
class FlanConfig(DatasetConfig):
    include_template_type: str = "zs_noopt"
    include_task_source: str = "P3,Flan2021"
    subsample_dev: int = None
    remove_phi_eval_tasks: bool = True


def filter_template_type(include_template_type, example):
    return example["template_type"] in include_template_type


def filter_task_source(include_task_source, example):
    return example["task_source"] in include_task_source


class FlanModule(DefaultDataModule):
    def setup_dataset(self):
        dataset = load_dataset(self.config.dataset)
        n_proc = int(os.environ.get("MTTL_NUM_PROC_DATASETS", 16))
        if "split" not in dataset.column_names["train"]:
            raise ValueError(
                "Dataset must have a 'split' column, try removing the dataset manually from the cache."
            )

        if self.config.include_template_type != "*":
            dataset = dataset.filter(
                partial(
                    filter_template_type,
                    set(self.config.include_template_type.split(",")),
                ),
                num_proc=n_proc,
                desc="Filtering template types",
            )

        if self.config.include_task_source != "*":
            dataset = dataset.filter(
                partial(
                    filter_task_source, set(self.config.include_task_source.split(","))
                ),
                num_proc=n_proc,
                desc="Filtering task sources",
            )

        (
            self._task_names,
            self._task_to_id,
            train_dataset,
            _,
            _,
        ) = maybe_filter_hf_dataset_by_task(
            dataset, "task_name", self.config.finetune_task_name, n_proc=n_proc
        )

        if "split" in dataset.column_names["train"]:
            self.train_dataset = train_dataset.filter(
                lambda x: x["split"] == "train",
                num_proc=n_proc,
                desc="Creating train set",
            )
            self.dev_dataset = train_dataset.filter(
                lambda x: x["split"] == "validation",
                num_proc=n_proc,
                desc="Creating valid set",
            )
            self.test_dataset = train_dataset.filter(
                lambda x: x["split"] == "test",
                num_proc=n_proc,
                desc="Creating test set",
            )
        else:
            self.train_dataset, self.dev_dataset = self.create_train_valid_split(
                train_dataset
            )
            self.test_dataset = self.dev_dataset

        if self.config.subsample_dev:
            logger.info(
                f"subsampling the dev dataset to {self.config.subsample_dev} samples"
            )
            self.subsample_dataset("dev_dataset", self.config.subsample_dev)

        if self.config.remove_phi_eval_tasks:

            def is_phi2_eval_task(datapoint):
                eval_tasks = [
                    "hellaswag_1_1_0",
                    "ai2_arc_ARC_Challenge_1_0_0",
                    "ai2_arc_ARC_Easy_1_0_0",
                    "piqa_1_0_0",
                    "winogrande_1_1_0",
                    "bool_q_1_0_0",
                    "openbookqa_0_1_0",
                ]
                return not any(
                    eval_task == datapoint["task_name"] for eval_task in eval_tasks
                )

            self.train_dataset = self.train_dataset.filter(
                is_phi2_eval_task,
                num_proc=n_proc,
                desc="Filtering phi-2 eval tasks from training mixture.",
            )

        # Wrap the datasets to also return the task_id
        self.print_infos()


@dataclass
class T0FlatConfig(DatasetConfig):
    use_templates_as_tasks: bool = False


class T0FlatModule(DefaultDataModule):
    def setup_dataset(self):
        dataset = load_dataset(self.config.dataset)

        (
            self._task_names,
            self._task_to_id,
            train_dataset,
            _,
            _,
        ) = maybe_filter_hf_dataset_by_task(
            dataset, "task_name", self.config.finetune_task_name
        )

        if self.config.use_templates_as_tasks:

            def concat_templates_and_task(example):
                example["task_name"] = (
                    example["task_name"]
                    + "/"
                    + example["template_type"].strip().replace(" ", "_")
                )
                return example

            train_dataset = train_dataset.map(
                concat_templates_and_task,
                num_proc=os.environ.get("MTTL_NUM_PROC_DATASETS", 16),
            )

            self._task_names = sorted(list(set(train_dataset["task_name"])))
            self._task_to_id = {
                task_name: i for i, task_name in enumerate(self._task_names)
            }

        self.train_dataset, self.dev_dataset = self.create_train_valid_split(
            train_dataset
        )
        self.test_dataset = self.dev_dataset
        self.print_infos()
