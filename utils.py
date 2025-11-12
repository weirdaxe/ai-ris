# # utils.py
# import json
# import os
# import errno
# from typing import Dict, Any

# utils.py
import json
import os
import errno
from typing import Dict, Any

def ensure_dir(path: str):
    os.makedirs(path, exist_ok=True)

def atomic_write_json(path: str, data: Dict[Any, Any], indent: int = 4):
    """
    Write JSON to disk atomically, flush + fsync to ensure durability.
    """
    ensure_dir(os.path.dirname(path) or ".")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)

def load_json_safe(path: str) -> Dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}

def ensure_file_exists(path: str):
    if not os.path.exists(path):
        ensure_dir(os.path.dirname(path) or ".")
        atomic_write_json(path, {})

def sanitize_site_name(name: str) -> str:
    # Basic sanitize: replace spaces with underscore and remove problematic characters
    valid = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name.strip())
    return valid or "site"

def config_paths_for_site(site_name: str):
    """
    Return (final_config_path, meta_path) for a site.
    final_config_path is what links.json will point to.
    """
    sanitized = sanitize_site_name(site_name)
    configs_dir = "configs"
    ensure_dir(configs_dir)
    final = os.path.join(configs_dir, f"{sanitized}_scrape_config.json")
    return final

# def ensure_dir(path: str):
#     os.makedirs(path, exist_ok=True)

# def atomic_write_json(path: str, data: Dict[Any, Any], indent: int = 4):
#     """
#     Write JSON to disk atomically, flush + fsync to ensure durability.
#     """
#     ensure_dir(os.path.dirname(path) or ".")
#     tmp = path + ".tmp"
#     with open(tmp, "w", encoding="utf-8") as f:
#         json.dump(data, f, indent=indent, ensure_ascii=False)
#         f.flush()
#         os.fsync(f.fileno())
#     os.replace(tmp, path)

# def load_json_safe(path: str) -> Dict:
#     if not os.path.exists(path):
#         return {}
#     try:
#         with open(path, "r", encoding="utf-8") as f:
#             return json.load(f)
#     except (json.JSONDecodeError, OSError):
#         return {}

# def ensure_file_exists(path: str):
#     if not os.path.exists(path):
#         ensure_dir(os.path.dirname(path) or ".")
#         atomic_write_json(path, {})

# def sanitize_site_name(name: str) -> str:
#     # Basic sanitize: replace spaces with underscore and remove problematic characters
#     valid = "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in name.strip())
#     return valid or "site"

# def config_paths_for_site(site_name: str):
#     """
#     Return (final_config_path, meta_path) for a site.
#     final_config_path is what links.json will point to.
#     """
#     sanitized = sanitize_site_name(site_name)
#     configs_dir = "configs"
#     ensure_dir(configs_dir)
#     final = os.path.join(configs_dir, f"{sanitized}_scrape_config.json")
#     return final
