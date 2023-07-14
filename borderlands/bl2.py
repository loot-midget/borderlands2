import argparse
from typing import List

from borderlands.bl2_data import (
    LEVELS_TO_TRAVEL_STATION_MAP,
    NO_EXPLORATION_CHALLENGE_LEVELS,
    create_bl2_challenges,
    BL2_SAVE_STRUCTURE,
)
from borderlands.savefile import App


class AppBL2(App):
    """
    Our main application class for Borderlands 2
    """

    def __init__(self, args: List[str]) -> None:
        super().__init__(
            args=args,
            item_struct_version=7,
            game_name='Borderlands 2',
            item_prefix='BL2',
            max_level=80,
            black_market_keys=(
                'backpack',
                'bank',
                'grenade',
                'launcher',
                'pistol',
                'rifle',
                'shotgun',
                'smg',
                'sniper',
            ),
            black_market_ammo={
                'grenade': [3, 4, 5, 6, 7, 8, 9, 10],
                'launcher': [12, 15, 18, 21, 24, 27, 30, 33],
                'pistol': [200, 300, 400, 500, 600, 700, 800, 900],
                'rifle': [280, 420, 560, 700, 840, 980, 1120, 1260],
                'shotgun': [80, 100, 120, 140, 160, 180, 200, 220],
                'smg': [360, 540, 720, 900, 1080, 1260, 1440, 1620],
                'sniper': [48, 60, 72, 84, 96, 108, 120, 132],
            },
            unlock_choices=['slaughterdome', 'tvhm', 'uvhm', 'challenges', 'ammo'],
            levels_to_travel_station_map=LEVELS_TO_TRAVEL_STATION_MAP,
            no_exploration_challenge_levels=NO_EXPLORATION_CHALLENGE_LEVELS,
            challenges=create_bl2_challenges(),
            save_structure=BL2_SAVE_STRUCTURE,
        )

    @staticmethod
    def setup_currency_args(parser) -> None:
        """
        Adds the options we're using to control currency
        """

        parser.add_argument(
            '--eridium',
            type=int,
            help='Eridium to set for character',
        )

        parser.add_argument(
            '--seraph',
            type=int,
            help='Seraph crystals to set for character',
        )

        parser.add_argument(
            '--torgue',
            type=int,
            help='Torgue tokens to set for character',
        )

    @staticmethod
    def oplevel(value):
        """
        Helper function for argparse which requires a valid Overpower level
        """
        try:
            intval = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError('OP Levels must be from 0 to 10')
        if intval < 0 or intval > 10:
            raise argparse.ArgumentTypeError('OP Levels must be from 0 to 10')
        return intval

    def setup_game_specific_args(self, parser):
        """
        Adds BL2-specific arguments
        """

        parser.add_argument(
            '--oplevel',
            type=AppBL2.oplevel,
            help='OP Level to unlock (will also unlock TVHM/UVHM if not already unlocked)',
        )
