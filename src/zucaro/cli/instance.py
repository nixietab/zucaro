import functools
import asyncio

import click

from zucaro.account import AccountError
from zucaro.cli.utils import pass_account_manager, pass_instance_manager, pass_launcher
from zucaro.logging import logger
from zucaro.utils import Directory, die, sanitize_name
from zucaro.java_manager import JavaManager


def instance_cmd(fn):
    @click.argument("instance_name")
    @functools.wraps(fn)
    def inner(*args, instance_name, **kwargs):
        return fn(*args, instance_name=sanitize_name(instance_name), **kwargs)

    return inner


def coro(f):
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        return asyncio.run(f(*args, **kwargs))
    return wrapper


@click.group()
def instance_cli():
    """Manage your instances."""
    pass


@instance_cli.command()
@instance_cmd
@click.argument("version", default="latest")
@pass_instance_manager
def create(im, instance_name, version):
    """Create a new instance."""
    if im.exists(instance_name):
        logger.error("An instance with that name already exists.")
        return
    im.create(instance_name, version)


@instance_cli.command()
@pass_instance_manager
def list(im):
    """Show a list of instances."""
    print("\n".join(im.list()))


@instance_cli.command()
@instance_cmd
@pass_instance_manager
def delete(im, instance_name):
    """Delete the instance (from disk)."""
    if im.exists(instance_name):
        im.delete(instance_name)
    else:
        logger.error("No such instance exists.")


@instance_cli.command()
@instance_cmd
@click.option("--verify", is_flag=True, default=False)
@click.option("-a", "--account", default=None)
@click.option("--version-override", default=None)
@click.option("--java", default=None, help="Custom Java directory")
@click.option("--manage-java", is_flag=True, default=False,
              help="Use Adoptium to automatically manage Java versions")
@click.option("--assigned-ram", default=None, help="Amount of RAM to assign to the game (e.g. '2G' for 2GB)")
@pass_instance_manager
@pass_account_manager
@coro
async def launch(am, im, instance_name, account, version_override, verify, java, manage_java, assigned_ram):
    """Launch the instance."""
    if account is None:
        account = am.get_default()
    else:
        account = am.get(account)
    if not im.exists(instance_name):
        logger.error("No such instance exists.")
        return
    inst = im.get(instance_name)
    
    # If assigned_ram is provided, temporarily override the memory settings
    if assigned_ram:
        original_min = inst.config["java.memory.min"]
        original_max = inst.config["java.memory.max"]
        inst.config["java.memory.min"] = assigned_ram
        inst.config["java.memory.max"] = assigned_ram
    
    try:
        await inst.launch(account, version_override, verify_hashes=verify, 
                         custom_java=java, manage_java=manage_java)
    except AccountError as e:
        logger.error("Not launching due to account error: {}".format(e))
    finally:
        # Restore original memory settings if they were overridden
        if assigned_ram:
            inst.config["java.memory.min"] = original_min
            inst.config["java.memory.max"] = original_max


@instance_cli.command("natives")
@instance_cmd
@pass_instance_manager
@coro
async def extract_natives(im, instance_name):
    """Extract natives and leave them on disk."""
    if not im.exists(instance_name):
        die("No such instance exists.")
    inst = im.get(instance_name)
    await inst.extract_natives()


@instance_cli.command("dir")
@click.argument("instance_name", required=False)
@pass_instance_manager
@pass_launcher
def _dir(launcher, im, instance_name):
    """Print root directory of instance."""
    if not instance_name:
        # TODO
        print(launcher.get_path(Directory.INSTANCES))
    else:
        instance_name = sanitize_name(instance_name)
        print(im.get_root(instance_name))


@instance_cli.command("rename")
@instance_cmd
@click.argument("new_name")
@pass_instance_manager
def rename(im, instance_name, new_name):
    """Rename an instance."""
    new_name = sanitize_name(new_name)
    if im.exists(instance_name):
        if im.exists(new_name):
            die("Instance with target name already exists.")
        im.rename(instance_name, new_name)
    else:
        die("No such instance exists.")


@instance_cli.group("config")
@instance_cmd
@pass_instance_manager
@click.pass_context
def config_cli(ctx, im, instance_name):
    """Configure an instance."""
    if im.exists(instance_name):
        ctx.obj = im.get(instance_name).config
    else:
        die("No such instance exists.")


@config_cli.command("show")
@click.pass_obj
def config_show(config):
    """Print the current instance config."""
    for k, v in config.items():
        print("{}: {}".format(k, v))


@config_cli.command("set")
@click.argument("key")
@click.argument("value")
@click.pass_obj
def config_set(config, key, value):
    """Set an instance config value."""
    config[key] = value


@config_cli.command("get")
@click.argument("key")
@click.pass_obj
def config_get(config, key):
    """Print an instance config value."""
    try:
        print(config[key])
    except KeyError:
        print("No such item.")


@config_cli.command("delete")
@click.argument("key")
@click.pass_obj
def config_delete(config, key):
    """Delete a key from the instance config."""
    try:
        del config[key]
    except KeyError:
        print("No such item.")


def register_instance_cli(zucaro_cli):
    zucaro_cli.add_command(instance_cli, name="instance")