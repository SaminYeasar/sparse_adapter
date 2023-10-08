import torch
import sys
import tqdm
import numpy as np
import os
import gc
import re
import json
from datasets import load_dataset
from vllm import LLM, SamplingParams
from dataclasses import dataclass, field
from mmlu_subject_configs import SUB_10, SUB_10_LAST
import click
import random
from abc import ABC, abstractmethod, abstractproperty

sys.path.append("../../")
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

import openai
from mttl.models.adapters import LoRA
from mttl.utils import setup_logging
from mttl.dataloader.platypus_dataset_reader import InversePlatypusTemplate
from typing import List
import time

openai.api_key = os.getenv("AZURE_OPENAI_KEY")
openai.api_base = os.getenv(
    "AZURE_OPENAI_ENDPOINT"
)  # your endpoint should look like the following https://YOUR_RESOURCE_NAME.openai.azure.com/
openai.api_type = "azure"
openai.api_version = "2023-05-15"  # this may change in the future


def retry_with_exponential_backoff(
    func,
    initial_delay: float = 1,
    exponential_base: float = 2,
    jitter: bool = True,
    max_retries: int = 200,
    errors: tuple = (openai.error.RateLimitError,),
):
    """Retry a function with exponential backoff."""

    def wrapper(*args, **kwargs):
        # Initialize variables
        num_retries = 0
        delay = initial_delay

        # Loop until a successful response or max_retries is hit or an exception is raised
        while num_retries <= max_retries:
            try:
                return func(*args, **kwargs)

            # Retry on specified errors
            except errors as e:
                # Increment retries
                num_retries += 1

                # Check if max retries has been reached
                if num_retries > max_retries:
                    raise Exception(
                        f"Maximum number of retries ({max_retries}) exceeded."
                    )

                # Increment the delay
                delay *= exponential_base * (1 + jitter * random.random())

                # Sleep for the delay
                time.sleep(delay)

            # Raise exceptions for any errors not specified
            except Exception as e:
                raise e

    return wrapper


class InstructionsGenerator(ABC):
    @dataclass
    class Response:
        outputs: List[str] = field(default_factory=list)
        contexts: List[str] = field(default_factory=list)
        responses: List[str] = field(default_factory=list)
        cumulative_logprobs: List[float] = field(default_factory=list)
        inst_index_for_context: List[int] = field(default_factory=list)

    @abstractproperty
    def model_name(self):
        pass


class InversePlatypusLLM(InstructionsGenerator, LLM):
    def __init__(self, *args, **kwargs):
        LLM.__init__(self, *args, **kwargs)
        self.template = InversePlatypusTemplate()

    @property
    def model_name(self):
        return self.llm_engine.model_config.model

    def generate_reverse_prompt(self, inputs):
        return self.template.apply(inputs)

    def generate(self, templated_contexts, sampling_params, **kwargs):
        results = InstructionsGenerator.Response()
        responses = super().generate(templated_contexts, sampling_params, **kwargs)
        for response in responses:
            if len(response.outputs[0].token_ids) > 0:
                results.outputs.append(response.outputs[0].text)
                results.cumulative_logprobs.append(
                    response.outputs[0].cumulative_logprob
                    / (len(response.outputs[0].token_ids) + 1e-10)
                )
        return results


class OpenAI(InstructionsGenerator):
    def __init__(
        self, model_name="text-davinci-003", n_gen_instructions_per_context=2, n_shot=2
    ):
        self.batch_size = 10
        self.n_shot = n_shot
        self.n_instructions_per_context = n_gen_instructions_per_context
        self._model_name = model_name
        assert n_shot > 0
        self._sys_prompt = f"You are a helpful and precise assistant for generating instructions for given user responses."

    def task_description(self, bs, icl):
        td = f"You are given a set of {bs} samples formated as json file. For each of theese samples, you are given a context in field 'context'.\
            \nYour task is to provide {self.n_instructions_per_context} complete instructions to which the answer is contained in this context. Generated instructions must contains all the nccessary information to answer them without assuming access to the context.\
            \nThe format, length and style of the instructions should be similar to the following instruction examples:"
        for ex in icl:
            td += f"\n\n### Instruction example:\n{ex}\n"

        td += f"\nFor each of the {self.n_instructions_per_context} instructions you generate, also provide a concise reponse that can be inferred from the 'context'.\
            \nRemember, it is very important to keep the tone, format, style and length of tour generated instruction as similar as possible to the instruction examples previosuly provided.\
            \nYour output must be formatted as a valid json file. For each of {bs} samples in the input, inlcude an entry in your output with the key corresponding to the index of the sample. Each such entry must include the following fields: 'instructions': <list of your generated intructions>, 'responses: <list of your generated responses>'\
            \n Here is the json formatted input:\n"
        return td

    def sys_prompt(self, icl):
        prompt = self._sys_prompt
        for ex in icl:
            prompt += f"\n\n### Instruction example:\n{ex}\n"
        return prompt

    @property
    def model_name(self):
        return self._model_name

    @retry_with_exponential_backoff
    def api_generation(
        self,
        messages: str,
        *args,
        **kwargs,
    ):
        responses = [
            openai.ChatCompletion.create(
                messages=messages,
                deployment_id=self.model_name,
                *args,
                **kwargs,
            )
        ]
        time.sleep(3)  # Preventing rate limits
        return responses

    def _apply_prompt_template(self, dict_values):
        instruction, input, output = (
            dict_values["instruction"],
            dict_values["input"],
            dict_values["output"],
        )
        prompt = ""
        if "icl_examples" in dict_values.keys():
            icl_examples = dict_values["icl_examples"]
            for i, icl_example in enumerate(icl_examples):
                prompt += f"\n\n### Instruciton example {i}: {icl_example}"
        prompt += f"\n\n### Context:\n{output}\n\n### Your response:\n"
        return prompt

    def generate_reverse_prompt(self, inputs):
        return self._apply_prompt_template(inputs)

    @staticmethod
    def process_outputs(output_json):
        # errors: tuple = (ValueError,)
        instructions = []
        responses = []
        for _, sample in output_json.items():
            instructions += sample["instructions"]
            responses += sample["responses"]

        return instructions, responses

    def extract_icl_examples(self, context):
        icl, example = re.split(r"\n\n### Context:\n", context)
        icl = re.split(r"\n\n### Instruciton example \d+:", icl)
        icl = [i.strip() for i in icl if len(i.strip()) > 0]
        return icl, example

    def process_batch(self, batch, icl_instructions):
        sys_prompt = self._sys_prompt
        prompt_task = self.task_description(
            self.batch_size, icl_instructions[: self.n_shot]
        )
        batch_json = json.dumps(batch)
        prompt_task += batch_json
        prompt_task += "\n\n### Your response:\n"
        message = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt_task},
        ]
        response = self.api_generation(message)
        output = response[0].choices[0]["message"]["content"]
        errors = (json.decoder.JSONDecodeError,)
        try:
            output_json = json.loads(output)
        except errors as e:
            return [], []
        except Exception as e:
            raise e

        instructions, responses = self.process_outputs(output_json)
        return instructions, responses

    def generate(self, templated_contexts, sampling_params, **kwargs):
        results = InstructionsGenerator.Response()
        icl_instructions = []
        batch = []
        for context in tqdm.tqdm(templated_contexts):
            icl, example = self.extract_icl_examples(context)
            icl_instructions += icl
            batch.append(
                {"context": example.replace("\n\n### Your response:\n", "").strip()}
            )
            if len(batch) == self.batch_size:
                instructions, responses = self.process_batch(batch, icl_instructions)
                for i, (ins, resp) in enumerate(zip(instructions, responses)):
                    results.outputs.append(ins)
                    # results.contexts.append(context)
                    results.responses.append(resp)
                    results.inst_index_for_context.append(i)

                icl_instructions = []
                batch = []
        if len(batch) > 0:
            instructions, responses = self.process_batch(batch, icl_instructions)
            for i, (ins, resp) in enumerate(zip(instructions, responses)):
                results.outputs.append(ins)
                # results.contexts.append(context)
                results.responses.append(resp)
                results.inst_index_for_context.append(i)

        return results


def sample_icl_examples(dataset, n_icl, use_options=True):
    dataset = dataset.shuffle()
    examples = []
    for i in range(n_icl):
        example = dataset[i]["input"]
        if use_options:
            example += " \nOptions:"
            for ans_option in ["A", "B", "C", "D"]:
                example += f" \n{ans_option}:" + dataset[i][ans_option]
            example = example.strip()
        examples.append(example)
    return examples


def generate_instructions_(
    llm: InstructionsGenerator,
    subject_names=SUB_10,
    max_tokens=128,
    temperature=0.7,
    top_p=0.9,
    num_contexts_per_document=4,
    max_context_length=512,
    output_filename="generated.jsonl",
    in_context_source="lukaemon/mmlu",
    n_icl=2,
    icl_use_out_options=True,
    subset=1.0,
):
    """
    To generate instructions, we take num_contexts_per_document chunks of length max_context_length from each document,
    then sample 1 instruction from each chunk.

    All instructions are appended as jsonl into output_filename.
    """
    random_sample = True
    template = InversePlatypusTemplate()
    sampling_params = SamplingParams(
        temperature=temperature, top_p=top_p, max_tokens=max_tokens
    )
    dataset = load_dataset("sordonia/my-wiki_mmlu_from_valid_all")["train"].to_pandas()

    for subject in subject_names:
        if n_icl > 0:
            icl_dataset = load_dataset(in_context_source, subject)["validation"]

        subject_data = dataset[dataset["subject"] == subject]

        contexts = []
        for text in tqdm.tqdm(subject_data["text"]):
            sentences = text.split(".")
            sentences = [
                sentence.strip().replace("\n", " ").replace("  ", " ")
                for sentence in sentences
                if len(sentence.strip()) > 0
            ]

            if not random_sample:
                append = False
                for sentence in sentences:
                    sentence = sentence + "."
                    if not append:
                        contexts.append(sentence)
                        append = True
                    else:
                        if (
                            len(contexts[-1].split()) + len(sentence.split())
                            < max_context_length
                        ):
                            contexts[-1] += " " + sentence
                        else:
                            contexts.append(sentence)
            else:
                for _ in range(num_contexts_per_document):
                    sent = ""
                    start = np.random.randint(0, len(sentences))

                    while len(sent.split()) < max_context_length:
                        sent += sentences[start] + ". "
                        start = (start + 1) % len(sentences)

                    # avoid context too long errors
                    if len(sent.split()) > max_context_length:
                        sent = " ".join(sent.split()[:max_context_length])

                    contexts.append(sent.strip())

        if subset > 0:
            contexts = contexts[: int(len(contexts) * subset)]

        templated_contexts = [
            llm.generate_reverse_prompt(
                {
                    "instruction": None,
                    "input": None,
                    "icl_examples": sample_icl_examples(
                        icl_dataset, n_icl, use_options=icl_use_out_options
                    )
                    if n_icl > 0
                    else None,
                    "output": sentence,
                }
            )
            for sentence in contexts
        ]
        responses = None
        outputs = llm.generate(templated_contexts, sampling_params, use_tqdm=True)
        responses = outputs.responses if len(outputs.responses) > 0 else responses
        contexts = outputs.contexts if len(outputs.contexts) > 0 else contexts
        inst_index_for_context = (
            outputs.inst_index_for_context
            if len(outputs.inst_index_for_context) > 0
            else None
        )
        outputs = outputs.outputs

        with open(output_filename, "a+") as f:
            import json

            for i, (output, context) in enumerate(zip(outputs, contexts)):
                f.write(
                    json.dumps(
                        {
                            "instruction": output,
                            "context": context,
                            "subject": subject,
                            "response": responses[i] if responses else None,
                            "author_instr": str(llm.model_name),
                            "inst_index_for_context": inst_index_for_context[i]
                            if inst_index_for_context
                            else None,
                        }
                    )
                )
                f.write("\n")


def generate_answers_(
    llm: InstructionsGenerator,
    instruction_json,
    max_tokens=1024,
    temperature=0.7,
    top_p=0.9,
):
    import json

    output_filename = instruction_json.replace(".json", "_answers.json")
    sampling_params = SamplingParams(
        temperature=temperature, top_p=top_p, max_tokens=max_tokens
    )

    with open(instruction_json, "r") as f:
        data = [json.loads(s) for s in f.readlines()]

    requests = []
    for instance in tqdm.tqdm(data):
        context = instance["context"]
        instruction = instance["instruction"]
        requests.append(
            f"Below is an instruction that describes a task. Write a response that appropriately completes the request.\n\n### Context:\n{context}\n\n### Instruction:\n{instruction}\n\n### Response:\n"
        )

    outputs = llm.generate(requests, sampling_params)
    normalized_cumulative_logprobs = outputs.cumulative_logprobs
    outputs = outputs.outputs

    with open(output_filename, "a+") as f:
        import json

        for output, instance, log_p in zip(
            outputs, data, normalized_cumulative_logprobs
        ):
            # instance["input"] = ""
            instance["response"] = (
                output.outputs[0].text if not isinstance(output, str) else output
            )
            instance["author_response"] = str(llm.model_name)
            instance["normalized_cumul_logprob_response"] = log_p
            f.write(json.dumps(instance))
            f.write("\n")
    return output_filename


def load_vllm_model(path, dtype="float16", tensor_parallel_size=2):
    llm = InversePlatypusLLM(
        model=path,
        dtype=dtype,
        tensor_parallel_size=tensor_parallel_size,
        gpu_memory_utilization=0.5,
    )
    return llm


def save_merged_model(mttl_ckpt_path, hf_path="/tmp/merged"):
    from expert_trainer import ExpertTrainer
    from mttl.utils import logger

    hf_path = os.path.join(hf_path, mttl_ckpt_path.replace("/", "_"))
    if not os.path.exists(hf_path):
        os.makedirs(hf_path)
    else:
        return hf_path

    model = ExpertTrainer.from_pretrained(
        mttl_ckpt_path,
        load_in_8bit=False,
        device_map={"": "cpu"},
    )
    if not model.hparams.model_modifier == "lora":
        raise NotImplementedError("Only LoRA models are supported.")

    merged = []
    for name, module in model.model.named_modules():
        for c_name, child in module.named_children():
            if isinstance(child, LoRA):
                child.merge_with_layer()
                setattr(
                    module,
                    c_name,
                    child.layer,
                )
                merged.append(name)

    logger.info("Merged LoRA layers: %s" % merged)
    logger.info("Saving merged model to: %s" % hf_path)

    model.model.save_pretrained(hf_path, save_full_model=True)
    logger.info("Saving tokenizer to: %s" % hf_path)
    model.tokenizer.save_pretrained(hf_path)

    # free all
    model.cpu()
    del model
    gc.collect()
    torch.cuda.empty_cache()
    return hf_path


@click.group()
def cli():
    setup_logging()


@cli.command("gena")
@click.option("--model-path", type=str, required=True)
@click.option("--instruction-json", type=str, required=True)
def generate_answers(model_path, instruction_json):
    model_path = save_merged_model(
        model_path, "/home/v-oostapenko/mttl_out/models/merged"
    )
    llm = load_vllm_model(model_path, tensor_parallel_size=1)
    output_filename = generate_answers_(llm, instruction_json=instruction_json)


@cli.command("merge-n-save")
@click.option("--mttl-checkpoint", type=str, required=True)
@click.option("--output-path", type=str, required=True)
def generate_instructions(mttl_checkpoint, output_path):
    save_merged_model(mttl_checkpoint, output_path)


@cli.command("geni")
@click.option("--model-path", type=str, required=True)
@click.option("--output-filename", type=str, required=True)
@click.option("--n_icl", type=int, required=False, default=5)
@click.option(
    "--icl-use-out-options",
    type=bool,
    required=False,
    default=True,
    help="if True, the output options of MMLU will be included into positive prompts examples.",
)
@click.option(
    "--subset-per-subject",
    type=float,
    required=False,
    default=-1,
    help="if > 0, this portion of subjects' data is processed.",
)
def generate_instructions(
    model_path, output_filename, n_icl, icl_use_out_options, subset_per_subject=-1
):
    if model_path in ["gpt-35-turbo", "gpt-4"]:
        llm = OpenAI(model_path, n_shot=n_icl)
    else:
        model_path = save_merged_model(
            model_path, "/home/v-oostapenko/mttl_out/models/merged"
        )
        llm = load_vllm_model(model_path, tensor_parallel_size=1)
    generate_instructions_(
        llm,
        output_filename=output_filename,
        n_icl=n_icl,
        icl_use_out_options=icl_use_out_options,
        subset=subset_per_subject,
    )


if __name__ == "__main__":
    cli()
