
from typing import Any



def flatten_dict(
    data: dict[str, Any], parent_keys: list[str] | None = None, prefix: str = ""
) -> dict[str, str]:
    """
    Recursively flattens a nested dictionary to generate environment variable keys following the specified format.
    Combines nested keys with underscores, converts to uppercase, and prepends a custom prefix if provided.

    Args:
        data: Nested dictionary to be flattened (parsed from JSON/YAML)
        parent_keys: List to track nested keys during recursion
        prefix: Custom prefix to be added to all generated keys

    Returns:
        Flattened dictionary with keys in PREFIX_KEY1_KEY2... format and string values
    """
    parent_keys = parent_keys or []
    flat_data = {}

    for key, value in data.items():
        # Clean and standardize key: convert to uppercase, replace spaces/hyphens with underscores
        clean_key = key.upper().replace(" ", "_").replace("-", "_")
        current_keys = [*parent_keys, clean_key]

        if isinstance(value, dict):
            # Recursively process nested dictionaries
            nested_flat = flatten_dict(value, current_keys, prefix)
            flat_data.update(nested_flat)
        else:
            # Construct full key name with prefix (if provided) and nested keys
            if prefix:
                full_key = f"{prefix.upper()}_{'_'.join(current_keys)}"
            else:
                full_key = "_".join(current_keys)

            # Process value: ensure string type, convert None to empty string
            flat_value = "" if value is None else str(value).strip()

            flat_data[full_key] = flat_value

    return flat_data
