import json
from functools import lru_cache
from importlib.resources import files
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

from emosa.agents import validate_bindings
from emosa.errors import EmosaError, Reason


@lru_cache
def validator(name):
    local = Path(__file__).resolve().parents[2] / "schemas" / f"{name}.schema.json"
    path = local if local.exists() else files("emosa").joinpath(f"contracts/{name}.schema.json")
    schema = json.loads(path.read_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate(name, value):
    errors = sorted(validator(name).iter_errors(value), key=lambda e: str(list(e.path)))
    if errors:
        # jsonschema's default message may echo secrets in rejected input.
        raise EmosaError(
            Reason.INVALID_INPUT,
            f"invalid {name} contract",
            path=list(errors[0].absolute_path),
            rule=errors[0].validator,
        )
    return value


def load(name, path):
    try:
        value = json.loads(Path(path).read_text())
    except (ValueError, OSError) as exc:
        raise EmosaError(Reason.INVALID_INPUT, f"cannot read {name} JSON") from exc
    validate(name, value)
    if name == "config":
        validate_bindings(value)
        pods = value["pods"]
        if len({p["pod_id"] for p in pods}) != len(pods):
            raise EmosaError(Reason.INVALID_INPUT, "duplicate pod identity")
        if len({p["endpoint"] for p in pods}) != len(pods):
            raise EmosaError(Reason.INVALID_INPUT, "endpoint cannot bind multiple pods")
        if value["backend_mode"] in {"hardware", "opensync-native"}:
            raise EmosaError(Reason.MISSING_PREREQUISITE, "M0/R0 target qualification is pending")
    return value
