from functools import wraps

from voluptuous import Schema

__all__ = [
    "request_mapping",
    "on_startup",
    "on_event",
    "action",
    "background_task",
    "parameters",
]

from aiohttp_auth import auth


def composed(*decs):
    def deco(f):
        for dec in reversed(decs):
            f = dec(f)
        return f

    return deco


def request_mapping(
    path, name=None, method="GET", auth_required=True, json_schema=None
):

    def on_http_request(path, name=None):
        def real_decorator(func):
            func.route = True
            func.path = path
            func.name = name
            func.method = method
            return func

        return real_decorator

    def validate_json_body(func):

        @wraps(func)
        async def wrapper(*args):

            if json_schema is not None:
                data = await args[-1].json()
                schema = Schema(json_schema)
                schema(data)

            return await func(*args)

        return wrapper

    if auth_required is True:
        return composed(
            on_http_request(path, name), auth.auth_required, validate_json_body
        )
    else:
        return composed(on_http_request(path, name), validate_json_body)


def on_event(topic):
    def real_decorator(func):
        func.eventbus = True
        func.topic = topic
        func.c = None
        return func

    return real_decorator


def action(key, parameters):
    def real_decorator(func):
        func.action = True
        func.key = key
        func.parameters = parameters
        return func

    return real_decorator


def resolve_action(instance, name):
    """The bound method for a *declared* action, or None.

    `call_action` used to do `instance.__getattribute__(name)(**parameter)`
    with `name` taken straight from a JSON request body, on endpoints some of
    which are `auth_required=False`. That is not "call an action", it is "call
    anything on this object by name, with arbitrary keyword arguments", on a
    box that switches several kilowatts.

    An action is already a declared thing: `@action(key, parameters)` sets
    `.action` on the function, and `plugin_controller` enumerates exactly those
    to tell the interface which actions exist. Anything not so declared was
    never offered to a caller and has no business being reachable by one.

    Resolved from the type rather than the instance, so an attribute in the
    instance dict cannot shadow the check.
    """
    if not isinstance(name, str) or not name:
        return None
    declared = getattr(type(instance), name, None)
    if declared is None or not getattr(declared, "action", False):
        return None
    bound = getattr(instance, name, None)
    return bound if callable(bound) else None


def normalize_action_parameters(parameter):
    """Whatever the caller sent, as keyword arguments.

    `**parameter` requires a mapping. Callers send `None` when an action takes
    no arguments, and `http_step` defaults to `[]`, both of which raise before
    the action is ever reached - so a parameterless action could not be invoked
    at all. The interface also describes parameters as a list of name/value
    objects, so accept that shape too.
    """
    if parameter is None:
        return {}
    if isinstance(parameter, dict):
        return {str(k): v for k, v in parameter.items()}
    if isinstance(parameter, (list, tuple)):
        out = {}
        for p in parameter:
            if isinstance(p, dict) and "name" in p:
                out[str(p["name"])] = p.get("value")
        return out
    return {}


def parameters(parameter):
    def real_decorator(func):
        func.cbpi_p = True
        func.cbpi_parameters = parameter
        return func

    return real_decorator


def background_task(name, interval):
    def real_decorator(func):
        func.background_task = True
        func.name = name
        func.interval = interval
        return func

    return real_decorator


def on_startup(name, order=0):
    def real_decorator(func):
        func.on_startup = True
        func.name = name
        func.order = order
        return func

    return real_decorator


def entry_exit(f):
    def new_f():

        f()

    return new_f
