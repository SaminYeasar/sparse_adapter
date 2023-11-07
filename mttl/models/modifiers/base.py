from torch import nn
import re
from mttl.utils import logger
from dataclasses import dataclass


class Adapter(nn.Module):
    @property
    def layer_name(self):
        if not hasattr(self, "__layer_name__"):
            raise ValueError(
                "Layer name not set, dependency injection not done properly?"
            )

        return self.__layer_name__


@dataclass
class ModifierConfig(object):
    modify_modules: str = ".*"
    modify_layers: str = ".*"


class ModifyMixin(nn.Module):
    @classmethod
    def modify_transformer(cls, transformer, config):
        return modify_with_adapter(transformer, config, cls)


def modify_with_adapter(transformer, config, adapter_klass):
    for m_name, module in dict(transformer.named_modules()).items():
        if re.fullmatch(config.modify_modules, m_name):
            for c_name, layer in dict(module.named_children()).items():
                if re.fullmatch(config.modify_layers, c_name):
                    logger.info(f"Patching {m_name}.{c_name}...")

                    setattr(
                        module,
                        c_name,
                        adapter_klass(config, layer),
                    )
    return transformer
