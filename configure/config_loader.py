import os
import yaml


def load_config(config_path: str = None) -> dict:
    """Load YAML config and expand ${VAR} from environment"""
    if config_path is None:
        config_path = r"configure/config.yaml" 

    with open(config_path, "r") as f:
        raw_cfg = yaml.safe_load(f)

    def expand_env(value):
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            env_var = value[2:-1]
            return os.getenv(env_var, "")
        return value

    def deep_expand(obj):
        if isinstance(obj, dict):
            return {k: deep_expand(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [deep_expand(v) for v in obj]
        else:
            return expand_env(obj)

    return deep_expand(raw_cfg)

