import json
import re
import torch
from typing import Dict
import re
from string import Template

from mttl.models.utils import download_from_hub
from mttl.utils import get_checkpoint_path, logger
from mttl.config import Config

from dataclasses import dataclass


@dataclass
class ExpertInfo:
    """
    Stuff that we want to save about experts but will never be passed from command line
    """

    parent_node: str = None
    expert_name: str = None
    expert_task_name: str = None


class ExpertConfig(Config):
    pass


@dataclass
class Expert:
    expert_config: ExpertConfig
    expert_weights: Dict[str, torch.Tensor]
    expert_info: ExpertInfo

    def dumps(self):
        return {
            "expert_config": self.expert_config.dumps(),
            "expert_info": self.expert_info.__dict__,
            "expert_weights": self.expert_weights,
        }

    @classmethod
    def loads(cls, ckpt):
        return cls(
            expert_config=ExpertConfig(
                kwargs=json.loads(ckpt["expert_config"]),
                silent=True,
                raise_error=False,
            ),
            expert_info=ExpertInfo(**ckpt["expert_info"]),
            expert_weights=ckpt["expert_weights"],
        )


class Node:
    def __init__(self, name):
        self.name = name
        self.children = []
        self._cached_instantiation = None

    def get_name(self, **kwargs):
        return self.name

    @classmethod
    def from_args(cls, name, graph, args=None):
        return Node(name)

    def collect_variables(self):
        vars = []
        if hasattr(self, "variables"):
            vars += self.variables
        if not self.children:
            return vars
        for child in self.children:
            vars += child.collect_variables()
        return vars

    def instantiate(self, *args, **kwargs):
        if self._cached_instantiation is not None:
            return self._cached_instantiation

        assert (
            len(self.children) <= 1
        ), "Node can only have one child for now, use operators instead."

        instantiation = []
        if not self.children:
            # consider this to be a leaf node
            # the name of leafs is their destination (path)
            instantiation = [load_expert(self.name)]
        else:
            # currently non-leaf nodes have at most one child
            instantiation = [self.children[0].instantiate(*args, **kwargs)[0]]
        return instantiation

    def __repr__(self):
        return self.name

    def __str__(self):
        return self.name


class OperatorNode(Node):
    def __init__(self, name):
        super().__init__(name)

    @classmethod
    def from_args(cls, args, graph):
        raise NotImplementedError


class LinearNode(OperatorNode):
    OPERATORTemplate = Template("linear(${arg})")
    ARGUMENT = Template("${name}:${weight}")

    @classmethod
    def check_name(cls, string):
        # given any string translate it into the format of the linear node
        # e.g. 'linear(sordonia/expert_llama2_13b_security_studies:5,sordonia/llama2-13b-platypus:$weight)' is already in the format of the linear node
        # sordonia/expert_llama2_13b_security_studies -> linear(sordonia/expert_llama2_13b_security_studies:1.0)
        if string.startswith("linear"):
            return string
        else:
            return LinearNode.OPERATORTemplate.substitute(
                arg=LinearNode.ARGUMENT.substitute(name=string, weight=1.0)
            )

    @classmethod
    def create_name(cls, string):
        """
        Find and replace variable names with their position in the connection
        """
        string = LinearNode.check_name(string)
        variable_names = re.findall(r"\$([a-zA-Z_][a-zA-Z0-9_]*)", string)
        for i, var_name in enumerate(variable_names):
            # replace variable names with their position
            string = re.sub(f"\${var_name}", f"${i}", string, count=1)
        return string

    @classmethod
    def get_varaible_name(cls, node_name, var_index):
        return f"{node_name}[{var_index}]"

    @classmethod
    def add_variables_and_weights_to_node(cls, node: Node, graph, args):
        """
        Append varibales and weights to the node given args
        """
        node.weights = {}
        node.variables = []

        node_args_pairs = args.split(",")
        for i, pair in enumerate(node_args_pairs):
            child_name, weight = pair.split(":")
            node.children.append(graph.get_or_create_node(child_name.strip()))
            # node.weights.append(float(weight.strip()))
            weight = weight.strip()
            if "$" not in weight:
                node.weights[child_name] = float(weight)
            else:
                node.variables.append(LinearNode.get_varaible_name(node.name, i))
        return node

    @classmethod
    def instantiate_name(cls, name_template, varaibles: list = [], **kwargs):
        """
        Puts varibales in the name template if variables are given
        """
        name = LinearNode.check_name(name_template)
        for i, v in enumerate(varaibles):
            name = name.replace(f"${i}", str(kwargs[v]), 1)
        return name

    # End of Language for LinearNode

    @classmethod
    def from_args(cls, name, graph, args=None):
        name = LinearNode.create_name(name)
        node = LinearNode(name)
        node = LinearNode.add_variables_and_weights_to_node(node, graph, args)
        return node

    def get_name(self, **kwargs):
        if len(self.variables) == 0 or len(kwargs) == 0:
            return self.name
        return LinearNode.instantiate_name(self.name, self.variables, **kwargs)

    def instantiate(self, *args, **kwargs):
        if self._cached_instantiation is not None:
            return self._cached_instantiation

        instantiation = {}
        first_module = None
        for node in self.children:
            instantiation[node.name] = node.instantiate(*args, **kwargs)[0]
            first_module = (
                instantiation[node.name] if first_module is None else first_module
            )

        # now, merge with a given importance weight
        assert len(instantiation) == len(self.weights) + len(self.variables)

        merged_weights = {}
        for i, (name, expert) in enumerate(instantiation.items()):
            if name in self.weights:
                weight = self.weights[name]
            else:
                param_name = f"{self.name}[{i}]"
                weight = kwargs.get(param_name, None)
                assert (
                    weight is not None
                ), f"Must pass the weight for node {param_name} to be able to instantiate"

            for k, v in expert.expert_weights.items():
                value = v * torch.tensor(weight, dtype=v.dtype)
                if k in merged_weights:
                    merged_weights[k] += value
                else:
                    merged_weights[k] = value

        exp_info: ExpertInfo = first_module.expert_info
        config: ExpertConfig = first_module.expert_config
        exp_info.parent_node = self.get_name(**kwargs)
        return [
            Expert(
                expert_config=config,
                expert_weights=merged_weights,
                expert_info=exp_info,
            )
        ]

    def __repr__(self):
        return "linear({})".format(
            ", ".join(["{}:{}".format(n, w) for n, w in zip(self.nodes, self.weights)])
        )


class ModuleGraph:
    # Operator-to-class mapping
    OPERATOR_CLASSES = {None: Node, "linear": LinearNode}

    def __init__(self):
        self.nodes = {}

    @classmethod
    def from_module_dict(cls, module_dict: dict):
        s = ""
        for module, dest in module_dict.items():
            s += f"{module} -> {LinearNode.instantiate_name(dest, )}; "
        return cls.from_string(s)

    def get_or_create_node(self, node_name, node_type=None, args=None):
        if node_name not in self.nodes:
            node_class = self.OPERATOR_CLASSES[node_type]
            self.nodes[node_name] = node_class.from_args(node_name, self, args)
        return self.nodes[node_name]

    def dumps(self, **kwargs):
        graph_str = []
        for node_name, node in self.nodes.items():
            if not node.children:
                continue
            if isinstance(node, OperatorNode):
                continue
            graph_str.append(
                "{} -> {}".format(
                    node_name, ", ".join([n.get_name(**kwargs) for n in node.children])
                )
            )
        return "; ".join(graph_str)

    @classmethod
    def from_string(self, s):
        graph = ModuleGraph()
        parts = [p.strip() for p in s.split(";")]

        for part in parts:
            if "->" in part:
                source, targets = part.split("->")
                targets = targets.strip()
                source = source.strip()

                match_source = re.match(r"(\w+)\((.+)\)", source.strip())
                if match_source:
                    raise ValueError("Source cannot be an operator.")

                match_target = re.match(r"(\w+)\((.+)\)", targets.strip())
                source_node = graph.get_or_create_node(source)

                if match_target:  # This means there's an operator
                    operator = match_target.group(1)
                    args = match_target.group(2)

                    if operator not in self.OPERATOR_CLASSES:
                        raise ValueError(
                            f"Unknown operator: '{operator}' in segment '{part}'"
                        )

                    children = [
                        graph.get_or_create_node(
                            node_name=targets, node_type=operator, args=args
                        )
                    ]
                else:
                    children = [
                        graph.get_or_create_node(t.strip()) for t in targets.split(",")
                    ]
                source_node.children.extend(children)
        return graph

    @property
    def roots(self):
        parent_nodes = {}
        for _, parent_node in self.nodes.items():
            for children in parent_node.children:
                parent_nodes[children] = parent_node
        return set(self.nodes.values()) - set(parent_nodes.keys())

    @property
    def leaves(self):
        children_nodes = set()
        for _, parent_node in self.nodes.items():
            if not parent_node.children:
                children_nodes.add(parent_node)
        return children_nodes

    def create_modules(self, *args, **kwargs):
        root_modules = {}
        for root in self.roots:
            root_modules[root.name] = root.instantiate(*args, **kwargs)[0]
        return root_modules

    def get_variables(self):
        variables = []
        for root in self.roots:
            variables += root.collect_variables()
        return variables


def load_expert(
    expert_path: str,
    expert_name: str = None,
):
    # load the expert weights
    import os

    logger.info(f"Attempting to load expert from {expert_path}")
    if os.path.isfile(expert_path) or os.path.isdir(expert_path):
        expert_checkpoint = get_checkpoint_path(expert_path)
    else:
        expert_checkpoint = download_from_hub(expert_path)

    logger.info(f"Loading expert from {expert_checkpoint}...")
    expert_checkpoint = torch.load(expert_checkpoint, map_location="cpu")

    # remove tokenizer if ever is present
    if "tokenizer" in expert_checkpoint["hyper_parameters"]:
        del expert_checkpoint["hyper_parameters"]["tokenizer"]

    expert_config = ExpertConfig(
        kwargs=expert_checkpoint["hyper_parameters"],
        silent=True,
        raise_error=False,
    )
    expert_info = ExpertInfo(**expert_checkpoint.get("expert_info", {}))

    expert_name = expert_name or expert_config.expert_name
    if expert_name is None:
        if expert_config.finetune_task_name is not None:
            expert_name = expert_config.finetune_task_name
        else:
            expert_name = os.path.basename(expert_path)
        logger.info(
            "Assigning expert name, not found in checkpoint: {}".format(expert_name)
        )

    expert_config.expert_name = expert_name

    expert_weights = expert_checkpoint["state_dict"]
    expert_weights = {k.replace("model.", "", 1): v for k, v in expert_weights.items()}
    return Expert(expert_config, expert_weights, expert_info)


if __name__ == "__main__":
    # Example usage:
    s = """
    security_studies -> B;
    B -> linear(sordonia/llama2-13b-platypus:0.5, sordonia/expert_llama2_13b_security_studies:3);
    C -> linear(B:0.5);
    default -> C
    """
    s = """    
    Variables:
    - if a weight starts with $ it is considered as a variale
    - variables will be stored in lInearNodes under the name of the linear connection (e.g. "linear(a:5,b:$weight)" ) + the index of the varable in the connections, e.g. "linear(a:5,b:$weight)[1]" since $weight is the second variable in the connection
    - a graph with variables cannot be instantiated without passing the values for the variables to the instantiate method
    
    security_studies -> linear(sordonia/expert_llama2_13b_security_studies:5,sordonia/llama2-13b-platypus:$weight);
    security_studies2 -> linear(sordonia/expert_llama2_13b_security_studies:1);    
    security_studies3 -> linear(sordonia/expert_llama2_13b_security_studies:$weight_blabla);
    """

    graph = ModuleGraph.from_string(s)
    print(graph)
    print(graph.roots)
    print(graph.leaves)
    print(graph.dumps())
    vars = graph.get_variables()
    print(vars)
    print(
        graph.dumps(
            **{
                "linear(sordonia/expert_llama2_13b_security_studies:5,sordonia/llama2-13b-platypus:$0)[1]": 0,
                "linear(sordonia/expert_llama2_13b_security_studies:$0)[0]": 1,
            }
        )
    )
    print(graph.dumps(**{v: i for i, v in enumerate(vars)}))
    print(graph.create_modules(**{v: 1 for v in vars}).keys())
