from .account import register_account_cli
from .config import register_config_cli
from .instance import register_instance_cli
from .main import zucaro_cli
from .mod import register_mod_cli
from .play import register_play_cli
from .version import register_version_cli

register_account_cli(zucaro_cli)
register_version_cli(zucaro_cli)
register_instance_cli(zucaro_cli)
register_config_cli(zucaro_cli)
register_mod_cli(zucaro_cli)
register_play_cli(zucaro_cli)
