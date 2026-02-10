import getpass

import click

from zucaro.account import OfflineAccount, OnlineAccount
from zucaro.cli.utils import (
    coro,
    pass_account_manager,
    pass_instance_manager,
    pass_launcher,
)
from zucaro.errors import AccountError


@click.command()
@click.argument("version", required=False)
@click.option("-a", "--account", "account_name")
@click.option("--verify", is_flag=True, default=False)
@click.option("--java", default=None, help="Custom Java directory")
@click.option("--manage-java", is_flag=True, default=False,
              help="Use Adoptium to automatically manage Java versions")
@click.option("--assigned-ram", default=None, help="Amount of RAM to assign to the game (e.g. '2G' for 2GB)")
@pass_instance_manager
@pass_account_manager
@pass_launcher
@coro
async def play(launcher, am, im, version, account_name, verify, java, manage_java, assigned_ram):
    """Play Minecraft without having to deal with stuff"""

    if account_name:
        account = am.get(account_name)
    else:
        try:
            account = am.get_default()
        except AccountError:
            username = input("Choose your account name:\n> ")
            email = input(
                "\nIf you have a Mojang account with a Minecraft license,\n"
                "enter your email. Leave blank if you want to play offline:\n> "
            )
            if email:
                account = OnlineAccount.new(am, username, email)
            else:
                account = OfflineAccount.new(am, username)
            am.add(account)
            if email:
                password = getpass.getpass("\nPassword:\n> ")
                await account.authenticate(password)

    if not im.exists("default"):
        im.create("default", "latest")

    inst = im.get("default")

    # If assigned_ram is provided, temporarily override the memory settings
    if assigned_ram:
        original_min = inst.config["java.memory.min"]
        original_max = inst.config["java.memory.max"]
        inst.config["java.memory.min"] = assigned_ram
        inst.config["java.memory.max"] = assigned_ram

    try:
        await inst.launch(account, version, verify_hashes=verify,
                         custom_java=java, manage_java=manage_java)
    finally:
        # Restore original memory settings if they were overridden
        if assigned_ram:
            inst.config["java.memory.min"] = original_min
            inst.config["java.memory.max"] = original_max

def register_play_cli(zucaro_cli):
    zucaro_cli.add_command(play)
