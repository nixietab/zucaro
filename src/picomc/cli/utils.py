import functools
import asyncio
import click

from picomc.launcher import Launcher

pass_launcher = click.make_pass_decorator(Launcher)


def pass_launcher_attrib(attr):
    def decorator(fn):
        @pass_launcher
        @functools.wraps(fn)
        def wrapper(launcher, *a, **kwa):
            x = getattr(launcher, attr)
            fn(x, *a, **kwa)

        return wrapper

    return decorator


def coro(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


pass_config_manager = pass_launcher_attrib("config_manager")
pass_account_manager = pass_launcher_attrib("account_manager")
pass_version_manager = pass_launcher_attrib("version_manager")
pass_instance_manager = pass_launcher_attrib("instance_manager")
pass_global_config = pass_launcher_attrib("global_config")