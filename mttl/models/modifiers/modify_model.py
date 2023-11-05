MODIFIERS = {}


def register_modifier(name):
    print("Registering modifier..." + name)

    def _thunk(fn):
        if name in MODIFIERS:
            raise ValueError(f"Cannot register duplicate model modifier ({name})")
        MODIFIERS[name] = fn
        return fn

    return _thunk


def modify_transformer(transformer, modifier_config):
    import mttl.models.modifiers.lora  # noqa: F401
    import mttl.models.modifiers.poly  # noqa: F401
    import mttl.models.modifiers.routing  # noqa: F401
    import mttl.models.modifiers.prompt_tuning  # noqa: F401
    import mttl.models.modifiers.llama_adapter  # noqa: F401

    # import mttl.models.modifiers.prefix_tuning # noqa: F401

    # create a shared container for the task id
    transformer.task_id_container = {}

    if modifier_config.model_modifier:
        if modifier_config.model_modifier in MODIFIERS:
            transformer = MODIFIERS[modifier_config.model_modifier](
                transformer, modifier_config
            )
        else:
            raise ValueError(
                f"Model modifier '{modifier_config.model_modifier}' not found."
            )
    return transformer
