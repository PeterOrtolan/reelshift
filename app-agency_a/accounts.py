import os
import json
import glob

DATA_DIR = os.environ.get("REELSHIFT_DATA_DIR", ".")
ACCOUNTS_DIR = os.path.join(DATA_DIR, "configs", "accounts")


def _account_path(model_name):
    return os.path.join(ACCOUNTS_DIR, f"{model_name}.json")


def _validate(cfg):
    if not isinstance(cfg, dict):
        raise ValueError("Account config must be a JSON object")

    required = ["model_name", "ig_user_id"]
    missing = [k for k in required if not cfg.get(k)]
    if missing:
        raise ValueError(f"Missing required fields: {missing}")

    if not isinstance(cfg["model_name"], str) or not cfg["model_name"].strip():
        raise ValueError("model_name must be a non-empty string")
    if not isinstance(cfg["ig_user_id"], (str, int)):
        raise ValueError("ig_user_id must be a string or number")

    cfg["model_name"] = cfg["model_name"].strip()
    cfg["ig_user_id"] = str(cfg["ig_user_id"]).strip()

    if not cfg["ig_user_id"].isdigit():
        raise ValueError("ig_user_id must be numeric")

    cfg.setdefault("share_to_feed", True)
    if not isinstance(cfg["share_to_feed"], bool):
        raise ValueError("share_to_feed must be true or false")

    if "ig_access_token" in cfg:
        if not isinstance(cfg["ig_access_token"], str) or not cfg["ig_access_token"].strip():
            raise ValueError("ig_access_token must be a non-empty string")
        cfg["ig_access_token"] = cfg["ig_access_token"].strip()

    if "proxy_url" in cfg:
        if cfg["proxy_url"] in (None, ""):
            cfg.pop("proxy_url")
        elif not isinstance(cfg["proxy_url"], str):
            raise ValueError("proxy_url must be a string")
        else:
            cfg["proxy_url"] = cfg["proxy_url"].strip()

    allowed = {"model_name", "ig_user_id", "share_to_feed", "ig_access_token", "proxy_url"}
    return {k: cfg[k] for k in allowed if k in cfg}


def add_account(json_text):
    try:
        cfg = json.loads(json_text)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON: {e}")

    cfg = _validate(cfg)

    path = _account_path(cfg["model_name"])
    if os.path.exists(path):
        raise ValueError(
            f"Account '{cfg['model_name']}' already exists. "
            f"Remove it first with /igremove {cfg['model_name']}"
        )

    os.makedirs(ACCOUNTS_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

    return cfg


def remove_account(model_name):
    path = _account_path(model_name.strip())
    if not os.path.exists(path):
        return False
    os.remove(path)
    return True


def list_accounts():
    if not os.path.isdir(ACCOUNTS_DIR):
        return []

    configs = []
    for path in sorted(glob.glob(os.path.join(ACCOUNTS_DIR, "*.json"))):
        try:
            with open(path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            cfg = _validate(cfg)
            configs.append(cfg)
        except Exception as e:
            print(f"[accounts] Skipping {path}: {e}")
    return configs


def get_account(model_name):
    for cfg in list_accounts():
        if cfg["model_name"] == model_name:
            return cfg
    return None
