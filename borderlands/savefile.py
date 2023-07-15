import base64
import binascii
import hashlib
import json
import math
import os
import random
import struct
import sys
import argparse
from io import BytesIO
from typing import List, Tuple, Dict, Set, Any

from borderlands.challenges import Challenge
from borderlands.datautil.bitstream import ReadBitstream, WriteBitstream
from borderlands.datautil.errors import BorderlandsError
from borderlands.datautil.huffman import (
    read_huffman_tree,
    make_huffman_tree,
    write_huffman_tree,
    invert_tree,
    huffman_decompress,
    huffman_compress,
)
from borderlands.datautil.lzo1x import lzo1x_decompress, lzo1x_1_compress
from borderlands.datautil.common import invert_structure, replace_raw_item_key

from borderlands.datautil.protobuf import (
    read_protobuf_value,
    read_repeated_protobuf_value,
    write_repeated_protobuf_value,
    read_protobuf,
    apply_structure,
    write_protobuf,
    remove_structure,
)
from borderlands.datautil.common import conv_binary_to_str, rotate_data_right, rotate_data_left, xor_data


class Config(argparse.Namespace):
    """
    Class to hold our configuration information.  Note that
    we're NOT using a separate class for BL2 and BLTPS configs,
    since so much of it is the same.
    """

    # Given by the user, booleans
    json = False
    bigendian = False
    verbose = True
    force = False
    copy_nvhm_missions = False
    print_unexplored_levels = False

    # Given by the user, strings
    import_items = None
    output = 'savegame'
    input_filename = '-'
    output_filename = '-'

    # Former 'modify' options
    name = None
    save_game_id = None
    level = None
    money = None
    eridium = None
    moonstone = None
    seraph = None
    torgue = None
    itemlevels = None
    backpack = None
    bank = None
    gunslots = None
    maxammo = None
    oplevel = None
    unlock = {}
    challenges = {}

    # Config options interpreted from the above
    endian = '<'
    changes = False
    show_info = False

    def finish(self, parser, app):
        """
        Some extra sanity checks on our options.  "parser" should
        be an active ArgumentParser object we can use to raise
        errors.  "app" is an App object which we use for a couple
        lookups.
        """

        # Endianness
        if self.bigendian:
            self.endian = '>'
        else:
            self.endian = '<'

        # If we're unlocking ammo, also set maxammo
        if 'ammo' in self.unlock:
            self.maxammo = True

        # Set our "changes" boolean -- first, args which take a value
        if any(
            var is not None
            for var in [
                self.backpack,
                self.bank,
                self.eridium,
                self.gunslots,
                self.itemlevels,
                self.level,
                self.maxammo,
                self.money,
                self.moonstone,
                self.name,
                self.oplevel,
                self.save_game_id,
                self.seraph,
                self.seraph,
                self.torgue,
            ]
        ):
            self.changes = True

        # Next, boolean args which are set to True
        if any([self.copy_nvhm_missions]):
            self.changes = True

        # Finally, any unlocks/challenges we mean to set
        if any(bool(var) for var in [self.unlock, self.challenges]):
            self.changes = True

        # Now set our "show_info" boolean.  Just a single boolean option, at the moment
        if any([self.print_unexplored_levels]):
            self.show_info = True

        # Can't read/write to the same file
        if self.input_filename == self.output_filename and self.input_filename != '-':
            parser.error('input_filename and output_filename cannot be the same file')

        # If the user specified --level, make sure it's from 1 to 80
        if self.level is not None:
            if self.level < 1:
                parser.error('level must be at least 1')
            if self.level > app.max_level:
                parser.error(f'level can be at most {app.max_level}')

        # Sort out 'backpack'
        if self.backpack is not None:
            if self.backpack == 'max':
                self.backpack = app.max_backpack_size
            else:
                try:
                    self.backpack = int(self.backpack)
                except ValueError:
                    parser.error(f'Backpack value "{self.backpack}" is not a number')
                if self.backpack > app.max_backpack_size:
                    self.backpack = app.max_backpack_size
                elif self.backpack < app.min_backpack_size:
                    self.backpack = app.min_backpack_size

        # Sort out bank
        if self.bank is not None:
            if self.bank == 'max':
                self.bank = app.max_bank_size
            else:
                try:
                    self.bank = int(self.bank)
                except ValueError:
                    parser.error(f'Backpack value "{self.bank}" is not a number')
                if self.bank > app.max_bank_size:
                    self.bank = app.max_bank_size
                elif self.bank < app.min_bank_size:
                    self.bank = app.min_bank_size


class DictAction(argparse.Action):
    """
    Custom argparse action to put list-like arguments into
    a dict (where the value will be True) rather than a list.
    This is probably implemented fairly shoddily.
    """

    def __init__(self, option_strings, dest, nargs=None, **kwargs):
        """
        Constructor, taken right from https://docs.python.org/2.7/library/argparse.html#action
        """
        if nargs is not None:
            raise ValueError('nargs is not allowed')
        super(DictAction, self).__init__(option_strings, dest, **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        """
        Actually setting a value.  Forces the attr into a dict if it isn't already.
        """
        arg_value = getattr(namespace, self.dest)
        if not isinstance(arg_value, dict):
            arg_value = {}
        arg_value[values] = True
        setattr(namespace, self.dest, arg_value)


class App(object):
    """
    Our main application class.
    """

    # These seem to be the same for both BL2 and BLTPS
    item_sizes = (
        (8, 17, 20, 11, 7, 7, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16, 16),
        (8, 13, 20, 11, 7, 7, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17, 17),
    )

    # Ditto
    item_header_sizes = (
        (("type", 8), ("balance", 10), ("manufacturer", 7)),
        (("type", 6), ("balance", 10), ("manufacturer", 7)),
    )

    min_backpack_size = 12
    max_backpack_size = 39
    min_bank_size = 6
    max_bank_size = 24

    # "laser" in here doesn't apply to BL2, but it won't hurt anything
    # because we process ammo pools based off the black market values,
    # which won't include lasers for BL2
    ammo_resources: Dict[str, Tuple[str, str]] = {
        'rifle': ('D_Resources.AmmoResources.Ammo_Combat_Rifle', 'D_Resourcepools.AmmoPools.Ammo_Combat_Rifle_Pool'),
        'shotgun': (
            'D_Resources.AmmoResources.Ammo_Combat_Shotgun',
            'D_Resourcepools.AmmoPools.Ammo_Combat_Shotgun_Pool',
        ),
        'grenade': (
            'D_Resources.AmmoResources.Ammo_Grenade_Protean',
            'D_Resourcepools.AmmoPools.Ammo_Grenade_Protean_Pool',
        ),
        'smg': ('D_Resources.AmmoResources.Ammo_Patrol_SMG', 'D_Resourcepools.AmmoPools.Ammo_Patrol_SMG_Pool'),
        'pistol': (
            'D_Resources.AmmoResources.Ammo_Repeater_Pistol',
            'D_Resourcepools.AmmoPools.Ammo_Repeater_Pistol_Pool',
        ),
        'launcher': (
            'D_Resources.AmmoResources.Ammo_Rocket_Launcher',
            'D_Resourcepools.AmmoPools.Ammo_Rocket_Launcher_Pool',
        ),
        'sniper': ('D_Resources.AmmoResources.Ammo_Sniper_Rifle', 'D_Resourcepools.AmmoPools.Ammo_Sniper_Rifle_Pool'),
        'laser': ('D_Resources.AmmoResources.Ammo_Combat_Laser', 'D_Resourcepools.AmmoPools.Ammo_Combat_Laser_Pool'),
    }

    # An equation for computing the XP required for a given level is
    # stated at http://borderlands.wikia.com/wiki/Experience_Points to
    # be (in Python terms):
    #
    #       math.ceil(60*(level**2.8) - 60)
    #
    # That works well for most of the levels in the game but it's not
    # perfect - it overshoots a bit towards the higher levels, which can
    # result in some annoying error messages in the game if you bring
    # a character to level 72 but have slightly too much XP.  Changing
    # math.ceil() to plain ol' int() works a bit better for that, actually.
    # Things get a bit better in Python if we use decimal.Decimal for
    # the numbers instead of relying on Python's native floats, but even
    # then it's not perfect.  I've tried out the calculation for level
    # 72 on a few different languages/calculators/platforms, and I think
    # the equation is just not exactly correct.
    #
    # So, what the heck - we'll just hardcode the XP requirements in here.
    #
    # Update in 2023, btw: I'd looked into this more thoroughly because
    # it's the same essential math used in BL3 / Wonderlands (including
    # Chaos XP in WL), and I was having the same XP drift over there that
    # I'd always seen while trying to compute this stuff in Python.  Well,
    # it turns out that the essential problem is that Python sort of
    # unavoidably does all the calculations using 64-bit doubles, instead
    # of 32-bit floats.  The difference in precision starts to add up over
    # time.  It turns out to be *real* difficult to force Python to use
    # floats in the background; you've gotta start doing stuff like `ctypes`
    # shenanigans to even have a prayer of it, and even then it's tricky
    # and might even require custom C extensions.  Anyway, I feel quite good
    # about these hardcodes.  Not worth the trouble in here, for sure.
    required_xp = [
        0,  # lvl 1
        358,  # lvl 2
        1241,  # lvl 3
        2850,  # lvl 4
        5376,  # lvl 5
        8997,  # lvl 6
        13886,  # lvl 7
        20208,  # lvl 8
        28126,  # lvl 9
        37798,  # lvl 10
        49377,  # lvl 11
        63016,  # lvl 12
        78861,  # lvl 13
        97061,  # lvl 14
        117757,  # lvl 15
        141092,  # lvl 16
        167206,  # lvl 17
        196238,  # lvl 18
        228322,  # lvl 19
        263595,  # lvl 20
        302190,  # lvl 21
        344238,  # lvl 22
        389873,  # lvl 23
        439222,  # lvl 24
        492414,  # lvl 25
        549578,  # lvl 26
        610840,  # lvl 27
        676325,  # lvl 28
        746158,  # lvl 29
        820463,  # lvl 30
        899363,  # lvl 31
        982980,  # lvl 32
        1071435,  # lvl 33
        1164850,  # lvl 34
        1263343,  # lvl 35
        1367034,  # lvl 36
        1476041,  # lvl 37
        1590483,  # lvl 38
        1710476,  # lvl 39
        1836137,  # lvl 40
        1967582,  # lvl 41
        2104926,  # lvl 42
        2248285,  # lvl 43
        2397772,  # lvl 44
        2553501,  # lvl 45
        2715586,  # lvl 46
        2884139,  # lvl 47
        3059273,  # lvl 48
        3241098,  # lvl 49
        3429728,  # lvl 50
        3625271,  # lvl 51
        3827840,  # lvl 52
        4037543,  # lvl 53
        4254491,  # lvl 54
        4478792,  # lvl 55
        4710556,  # lvl 56
        4949890,  # lvl 57
        5196902,  # lvl 58
        5451701,  # lvl 59
        5714393,  # lvl 60
        5985086,  # lvl 61
        6263885,  # lvl 62
        6550897,  # lvl 63
        6846227,  # lvl 64
        7149982,  # lvl 65
        7462266,  # lvl 66
        7783184,  # lvl 67
        8112840,  # lvl 68
        8451340,  # lvl 69
        8798786,  # lvl 70
        9155282,  # lvl 71
        9520931,  # lvl 72
        9895837,  # lvl 73
        10280103,  # lvl 74
        10673830,  # lvl 75
        11077120,  # lvl 76
        11490077,  # lvl 77
        11912801,  # lvl 78
        12345393,  # lvl 79
        12787955,  # lvl 80
    ]

    def pack_item_values(self, is_weapon, values):
        i = 0
        itembytes = bytearray(32)
        for value, size in zip(values, self.item_sizes[is_weapon]):
            if value is None:
                break
            j = i >> 3
            value = value << (i & 7)
            while value != 0:
                itembytes[j] |= value & 0xFF
                value = value >> 8
                j = j + 1
            i = i + size
        if (i & 7) != 0:
            value = 0xFF << (i & 7)
            itembytes[i >> 3] |= value & 0xFF
        return bytes(itembytes[: (i + 7) >> 3])

    def unpack_item_values(self, is_weapon, data):
        i = 8
        data = b' ' + data
        values = []
        end = len(data) * 8
        for size in self.item_sizes[is_weapon]:
            j = i + size
            if j > end:
                values.append(None)
                continue
            value = 0
            for b in data[j >> 3 : (i >> 3) - 1 : -1]:
                value = (value << 8) | b
            values.append((value >> (i & 7)) & ~(0xFF << size))
            i = j
        return values

    def wrap_item(self, is_weapon, values, key):
        item = self.pack_item_values(is_weapon, values)
        header = struct.pack(">Bi", (is_weapon << 7) | self.item_struct_version, key)
        padding = b"\xff" * (33 - len(item))
        h = binascii.crc32(header + b"\xff\xff" + item + padding) & 0xFFFFFFFF
        checksum = struct.pack(">H", ((h >> 16) ^ h) & 0xFFFF)
        body = xor_data(rotate_data_left(checksum + item, key & 31), key >> 5)
        return header + body

    def unwrap_item(self, data):
        version_type, key = struct.unpack(">Bi", data[:5])
        is_weapon = version_type >> 7
        raw = rotate_data_right(xor_data(data[5:], key >> 5), key & 31)
        return is_weapon, self.unpack_item_values(is_weapon, raw[2:]), key

    def unwrap_black_market(self, value):
        sdus = read_repeated_protobuf_value(value, 0)
        return dict(zip(self.black_market_keys, sdus))

    def wrap_black_market(self, value):
        sdus = [value[k] for k in self.black_market_keys[: len(value)]]
        return write_repeated_protobuf_value(sdus, 0)

    def unwrap_challenges(self, data):
        """
        Unwraps our challenge data.  The first ten bytes are a header:

            int32: Unknown, is always "4" on my savegames, though.
            int32: Size in bytes of all the challenges, plus two more bytes
                   for the next short
            short: Number of challenges

        Each challenge takes up a total of 12 bytes, so num_challenges*12
        should always equal size_in_bytes-2.

        The structure of each challenge is:

            byte: unknown, possibly at least part of an ID, but not unique
                  on its own
            byte: unknown, but is always (on my saves, anyway) 6 or 7.
            byte: unknown, but is always 1.
            int32: total value of the challenge, across all resets
            byte: unknown, but is always 1
            int32: previous, pre-challenge-reset value.  Will always be 0
                   until challenges have been reset at least once.

        The first two bytes of each challenge can be taken together, and if so, can
        serve as a unique identifier for the challenge.  I decided to read them in
        that way, as a short value.  I wasn't able to glean any pattern to whether
        a 6 or a 7 shows up in the second byte.

        Once your challenges have been reset in-game, the previous value is copied
        into that second int32, but the total value itself remains unchanged, so at
        that point you need to subtract previous_value from total_value to find the
        actual current state of the challenge (that procedure is obviously true
        prior to any resets, too, since previous_value is just zero in that case).

        It's also worth mentioning that challenge data keeps accumulating even
        after the challenge itself is completed, so the number displayed in-game
        for completed challenges is no longer accurate.

        """

        challenges = self.challenges

        (unknown, size_in_bytes, num_challenges) = struct.unpack(self.config.endian + 'IIH', data[:10])
        mydict = {'unknown': unknown}

        # Sanity check on size reported
        if (size_in_bytes + 8) != len(data):
            raise BorderlandsError(f'Challenge data reported as {size_in_bytes} bytes, but {len(data) - 8} bytes found')

        # Sanity check on number of challenges reported
        if (num_challenges * 12) != (size_in_bytes - 2):
            raise BorderlandsError(f'{num_challenges} challenges reported, but {size_in_bytes - 2} bytes of data found')

        # Now read them in
        mydict['challenges'] = []
        for challenge in range(num_challenges):
            idx = 10 + (challenge * 12)
            challenge_dict = dict(
                zip(
                    ['id', 'first_one', 'total_value', 'second_one', 'previous_value'],
                    struct.unpack(self.config.endian + 'HBIBI', data[idx : idx + 12]),
                )
            )
            mydict['challenges'].append(challenge_dict)

            if challenge_dict['id'] in challenges:
                info = challenges[challenge_dict['id']]
                challenge_dict['_id_text'] = info.id_text
                challenge_dict['_category'] = info.category.name
                challenge_dict['_name'] = info.name
                challenge_dict['_description'] = info.description

        return mydict

    def get_fully_explored_areas(self, player) -> list[str]:
        """
        Reuse converting full player data to json
        for simpler code
        """
        json_data = apply_structure(player, self.save_structure)
        if 'explored_areas' not in json_data:
            return []
        names = [x.decode('utf-8') for x in json_data['explored_areas']]
        return names

    def print_explored_levels(self, player) -> None:
        if not self.levels_to_travel_station_map:
            self.error(f'levels_to_travel_station_map is empty in class {self.__class__.__name__}')
            return

        unique_names = set(self.levels_to_travel_station_map.keys())
        explored_areas = self.get_fully_explored_areas(player)
        unexplored = set(unique_names) - set(explored_areas)
        labels = []
        for name in unexplored:
            travel_station = self.levels_to_travel_station_map.get(name, name)
            label = f'  {travel_station} ({name})'
            if name in self.no_exploration_challenge_levels:
                label += ' (does not contribute to Explorer-of-X achievement)'
            labels.append(label)
        if labels:
            self.notice('Not fully explored levels:')
            self.notice('\n'.join(sorted(labels)))
        self.notice(f'Total not fully explored levels: {len(unexplored)}')
        self.notice('')

    def wrap_challenges(self, data):
        """
        Re-wrap our challenge data.  See the notes above in unwrap_challenges for
        details on the structure.

        Note that we are trusting that the correct number of challenges are present
        in our data structure and setting size_in_bytes and num_challenges to match.
        Change the number of challenges at your own risk!
        """

        b = BytesIO()
        b.write(
            struct.pack(
                self.config.endian + 'IIH',
                data['unknown'],
                (len(data['challenges']) * 12) + 2,
                len(data['challenges']),
            )
        )
        save_challenges = data['challenges']
        for challenge in save_challenges:
            b.write(
                struct.pack(
                    self.config.endian + 'HBIBI',
                    challenge['id'],
                    challenge['first_one'],
                    challenge['total_value'],
                    challenge['second_one'],
                    challenge['previous_value'],
                )
            )
        return b.getvalue()

    def unwrap_item_info(self, value):
        is_weapon, item, key = self.unwrap_item(value)
        data = {"is_weapon": is_weapon, "key": key, "set": item[0], "level": [item[4], item[5]]}
        for i, (k, bits) in enumerate(self.item_header_sizes[is_weapon]):
            lib = item[1 + i] >> bits
            asset = item[1 + i] & ~(lib << bits)
            data[k] = {"lib": lib, "asset": asset}
        bits = 10 + is_weapon
        parts = []
        for value in item[6:]:
            if value is None:
                parts.append(None)
            else:
                lib = value >> bits
                asset = value & ~(lib << bits)
                parts.append({"lib": lib, "asset": asset})
        data["parts"] = parts
        return data

    def wrap_item_info(self, value):
        item = [value["set"]]
        for key, bits in self.item_header_sizes[value["is_weapon"]]:
            v = value[key]
            item.append((v["lib"] << bits) | v["asset"])
        item.extend(value["level"])
        bits = 10 + value["is_weapon"]
        for v in value["parts"]:
            if v is None:
                item.append(None)
            else:
                item.append((v["lib"] << bits) | v["asset"])
        return self.wrap_item(value["is_weapon"], item, value["key"])

    @staticmethod
    def unwrap_player_data(data):
        """
        Byte order on the few struct calls here appears to actually be
        hardcoded regardless of platform, so we're perhaps just leaving
        them, rather than using self.config.endian as we're doing elsewhere.
        I suspect this might actually be wrong, though, and just happens to
        work.
        """
        if data[:4] == "CON ":
            raise BorderlandsError(
                "You need to use a program like Horizon or Modio to extract the SaveGame.sav file first"
            )

        if data[:20] != hashlib.sha1(data[20:]).digest():
            raise BorderlandsError("Invalid save file")

        data = lzo1x_decompress(b'\xf0' + data[20:])
        size, wsg, version = struct.unpack('>I3sI', data[:11])
        if version != 2 and version != 0x02000000:
            raise BorderlandsError(f'Unknown save version {version}')

        if version == 2:
            crc, size = struct.unpack(">II", data[11:19])
        else:
            crc, size = struct.unpack("<II", data[11:19])

        bitstream = ReadBitstream(data[19:])
        tree = read_huffman_tree(bitstream)
        player = huffman_decompress(tree, bitstream, size)

        if (binascii.crc32(player) & 0xFFFFFFFF) != crc:
            raise BorderlandsError("CRC check failed")

        return player

    def wrap_player_data(self, player):
        """
        There's one call in here which had a hard-coded endian, as with
        unwrap_player_data above, so we're leaving that hardcoded for now.
        I suspect that it's wrong to be doing so, though.
        """
        crc = binascii.crc32(player) & 0xFFFFFFFF

        bitstream = WriteBitstream()
        tree = make_huffman_tree(player)
        write_huffman_tree(tree, bitstream)
        huffman_compress(invert_tree(tree), player, bitstream)
        data = bitstream.getvalue() + b"\x00\x00\x00\x00"

        header = struct.pack(">I3s", len(data) + 15, b'WSG')
        header += struct.pack(self.config.endian + "III", 2, crc, len(player))

        data = lzo1x_1_compress(header + data)[1:]

        return hashlib.sha1(data).digest() + data

    def show_save_info(self, data):
        """
        Shows information from file data, based on our config object.
        "data" should be the raw data from a save file.

        Note that if a user is both showing info and making changes,
        we're parsing the protobuf twice, since modify_save also does
        that.  Inefficiency!
        """

        player = read_protobuf(self.unwrap_player_data(data))
        if self.config.print_unexplored_levels:
            self.print_explored_levels(player)

    def modify_save(self, data):
        """
        Performs a set of modifications on file data, based on our
        config object.  "data" should be the raw data from a save
        file.

        Note that if a user is both showing info and making changes,
        we're parsing the protobuf twice, since show_save_info also does
        that.  Inefficiency!
        """

        player = read_protobuf(self.unwrap_player_data(data))
        if self.config.level is not None:
            if self.config.level < 1 or self.config.level > len(self.required_xp):
                self.error(f'Invalid character level specified: {self.config.level}')
            else:
                self.debug(f' - Updating to level {self.config.level}')
                lower = self.required_xp[self.config.level - 1]
                if self.config.level == len(self.required_xp):
                    if player[3][0][1] != lower:
                        player[3][0][1] = lower
                        self.debug(f'   - Also updating XP to {lower}')
                else:
                    upper = self.required_xp[self.config.level]
                    if player[3][0][1] < lower or player[3][0][1] >= upper:
                        player[3][0][1] = lower
                        self.debug(f'   - Also updating XP to {lower}')
                player[2] = [[0, self.config.level]]

        if any(
            [
                x is not None
                for x in [
                    self.config.money,
                    self.config.eridium,
                    self.config.moonstone,
                    self.config.seraph,
                    self.config.torgue,
                ]
            ]
        ):
            raw = player[6][0][1]
            b = BytesIO(raw)
            values = []
            while b.tell() < len(raw):
                values.append(read_protobuf_value(b, 0))
            if self.config.money is not None:
                self.debug(f' - Setting available money to {self.config.money}')
                values[0] = self.config.money
            if self.config.eridium is not None:
                self.debug(f' - Setting available eridium to {self.config.eridium}')
                values[1] = self.config.eridium
            if self.config.moonstone is not None:
                self.debug(f' - Setting available moonstone to {self.config.moonstone}')
                values[1] = self.config.moonstone
            if self.config.seraph is not None:
                self.debug(f' - Setting available Seraph Crystals to {self.config.seraph}')
                values[2] = self.config.seraph
            if self.config.torgue is not None:
                self.debug(f' - Setting available Torgue Tokens to {self.config.torgue}')
                values[4] = self.config.torgue
            player[6][0] = [0, values]

        # Note that this block should always come *after* the block which sets
        # character level, in case we've been instructed to set items to the
        # character's level.
        seen_level_1_warning = False
        if self.config.itemlevels is not None:
            if self.config.itemlevels > 0:
                self.debug(f' - Setting all items to level {self.config.itemlevels}')
                level = self.config.itemlevels
            else:
                level = player[2][0][1]
                self.debug(f' - Setting all items to character level ({level})')
            for field_number in (53, 54):
                for field in player[field_number]:
                    field_data = read_protobuf(field[1])
                    is_weapon, item, key = self.unwrap_item(field_data[1][0][1])
                    if self.config.forceitemlevels or item[4] > 1:
                        item = item[:4] + [level, level] + item[6:]
                        field_data[1][0][1] = self.wrap_item(is_weapon, item, key)
                        field[1] = write_protobuf(field_data)
                    else:
                        if item[4] == 1 and not seen_level_1_warning:
                            seen_level_1_warning = True
                            self.debug('   NOTICE: At least one item is level 1 and will not be updated.')
                            self.debug('   Use --forceitemlevels to update these items')

        # OP Level is stored in a weird little custom item.
        # See Gibbed.Borderlands2.FileFormats/SaveExpansion.cs for a bit more
        # rigorous example of how to process those properly.
        # Note that this needs to happen before the unlock section, since
        # it may trigger an unlock of UVHM if that wasn't already specified.
        if self.config.oplevel is not None:
            set_op_level = False
            self.debug(f' - Setting OP Level to {self.config.oplevel}')

            # Constructing the new value ahead of time since we'll need it
            # no matter what else happens below.
            # This little signed/unsigned dance is awful, but it lets us put the
            # value in as the same format we got it.  So: awesome. Byte order
            # shouldn't actually matter here so long as it's consistent.
            new_field_data = struct.unpack(
                '>Q', struct.pack('>q', -(4 | (max(0, min(self.config.oplevel, 0x7FFFFF)) << 8)))
            )[0]

            # Now actually get on with it
            if self.config.oplevel > 0:
                if player[7][0][1] < 2 and 'uvhm' not in self.config.unlock:
                    self.config.unlock['uvhm'] = True
                    self.debug('   - Also unlocking UVHM mode')
            for field in player[53]:
                field_data = read_protobuf(field[1])
                if 2 in field_data:
                    is_weapon, item, key = self.unwrap_item(field_data[1][0][1])
                    if item[0] == 255 and not any([val != 0 for val in item[1:]]):
                        idnum = (-field_data[2][0][1]) & 0xFF
                        # An ID of 4 is the one we're after
                        if idnum == 4:
                            field_data[2][0][1] = new_field_data
                            field[1] = write_protobuf(field_data)
                            set_op_level = True
                            break
            if not set_op_level:
                # If we didn't find an existing structure, we'll have to add our
                # own in
                self.debug('   - Creating new OP Level "virtual" item')
                # More magic from Gibbed code
                base_data = (
                    b"\x07\x00\x00\x00\x00\x39\x2a\xff"
                    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
                    + b"\x00\x00\x00\x00\x00\x00\x00\x00"
                )
                # noinspection PyDictCreation
                entry = {}
                entry[1] = [[2, base_data]]
                entry[2] = [[0, new_field_data]]
                entry[3] = [[0, 0]]
                entry[4] = [[0, 0]]
                player[53].append([2, write_protobuf(entry)])

        if self.config.backpack is not None:
            self.debug(f' - Setting backpack size to {self.config.backpack}')
            size = self.config.backpack
            sdu_size = int(math.ceil((size - self.min_backpack_size) / 3.0))
            self.debug(f'   - Setting SDU size to {sdu_size}')
            new_size = self.min_backpack_size + (sdu_size * 3)
            if size != new_size:
                self.debug(f'   - Resetting backpack size to {new_size} to match SDU count')
            slots = read_protobuf(player[13][0][1])
            slots[1][0][1] = new_size
            player[13][0][1] = write_protobuf(slots)
            s = read_repeated_protobuf_value(player[36][0][1], 0)
            player[36][0][1] = write_repeated_protobuf_value(s[:7] + [sdu_size] + s[8:], 0)

        if self.config.bank is not None:
            self.debug(f' - Setting bank size to {self.config.bank}')
            size = self.config.bank
            sdu_size = int(min(255, math.ceil((size - self.min_bank_size) / 2.0)))
            self.debug(f'   - Setting SDU size to {sdu_size}')
            new_size = self.min_bank_size + (sdu_size * 2)
            if size != new_size:
                self.debug(f'   - Resetting bank size to {new_size} to match SDU count')
            if 56 in player:
                player[56][0][1] = new_size
            else:
                player[56] = [[0, new_size]]
            s = read_repeated_protobuf_value(player[36][0][1], 0)
            if len(s) < 9:
                s = s + (9 - len(s)) * [0]
            player[36][0][1] = write_repeated_protobuf_value(s[:8] + [sdu_size] + s[9:], 0)

        if self.config.gunslots is not None:
            self.debug(f' - Setting available gun slots to {self.config.gunslots}')
            n = self.config.gunslots
            slots = read_protobuf(player[13][0][1])
            slots[2][0][1] = n
            if slots[3][0][1] > n - 2:
                slots[3][0][1] = n - 2
            player[13][0][1] = write_protobuf(slots)

        if self.config.copy_nvhm_missions:
            self.debug(' - Copying NVHM mission status to TVHM+UVHM')
            if 'uvhm' not in self.config.unlock:
                self.config.unlock['uvhm'] = True
                self.debug('   - Also unlocking UVHM mode')
            player[18][1][1] = player[18][0][1]
            player[18][2][1] = player[18][0][1]

        # Playing around with mission stuff.  Was thinking about including some
        # functions to mess around with level stats (like gamestage, etc) but will
        # probably not, in the end.  This was used to generate an index for my
        # big ol' collection of BL2/TPS savegames, though.
        #
        # I'm leaving this stuff in here just for my own purposes, in case I change
        # my mind, or need to re-do or tweak my savegame archives.
        # BL2: http://apocalyptech.com/games/bl-saves/
        # TPS: http://apocalyptech.com/games/bl-saves/tps.php
        # Github (both): https://github.com/apocalyptech/blsaves

        # if False:
        #     MSTAT = {
        #         0: 'Not Started',
        #         1: 'Active',
        #         2: 'Required Objectives Complete',
        #         3: 'Ready to Turn In',
        #         4: 'Completed',
        #         5: 'Failed',
        #     }
        #     # print('')
        #     # print('Last-visited teleporter: {}'.format(player[17][0][1].decode('latin1')))
        #     nvhm_proto = self.read_protobuf(player[18][0][1])
        #     cur_mission = nvhm_proto[2][0][1].decode('latin1')
        #     # print('All active missions:')
        #     active_missions = []
        #     turnin_missions = []
        #
        #     last_visited = 'None'
        #     # This exists in BL2 but not TPS
        #     if 17 in player:
        #         last_visited = player[17][0][1].decode('latin1')
        #     # This exists in TPS but not BL2
        #     if 8 in nvhm_proto:
        #         last_visited = nvhm_proto[8][0][1].decode('latin1')
        #
        #     if 3 in nvhm_proto:
        #         for mission_data in nvhm_proto[3]:
        #             mission = self.read_protobuf(mission_data[1])
        #             mission_name = mission[1][0][1].decode('latin1')
        #             mission_status = mission[2][0][1]
        #             gamestage = mission[11][0][1]
        #             if mission_status > 0:
        #                 if mission_status < 3:
        #                     active_missions.append(mission_name)
        #                 elif mission_status < 4:
        #                     turnin_missions.append(mission_name)
        #                     # print( ' * {} (level {}): {}'.format(mission_name, gamestage, MSTAT[mission_status]))
        #                     # if cur_mission == mission_name:
        #                     #    print('   ^^^^^^^^ currently-active mission')
        #     print(
        #         '{}|{}|{}|{}'.format(
        #             input_filename,
        #             last_visited,
        #             ','.join(active_missions),
        #             ','.join(turnin_missions),
        #         )
        #     )

        if self.config.unlock:
            if 'slaughterdome' in self.config.unlock:
                unlocked, notifications = b'', b''
                if 23 in player:
                    unlocked = player[23][0][1]
                if 24 in player:
                    notifications = player[24][0][1]
                self.debug(' - Unlocking Creature Slaughterdome')
                if 1 not in unlocked:
                    unlocked += b"\x01"
                if 1 not in notifications:
                    notifications += b"\x01"
                player[23] = [[2, unlocked]]
                player[24] = [[2, notifications]]
            if 'uvhm' in self.config.unlock:
                self.debug(' - Unlocking UVHM (and TVHM)')
                if player[7][0][1] < 2:
                    player[7][0][1] = 2
            elif 'tvhm' in self.config.unlock:
                self.debug(' - Unlocking TVHM')
                if player[7][0][1] < 1:
                    player[7][0][1] = 1
            if 'challenges' in self.config.unlock:
                self.debug(' - Unlocking all non-level-specific challenges')
                challenge_unlocks = [
                    apply_structure(read_protobuf(d[1]), self.save_structure[38][2]) for d in player[38]
                ]
                inverted_structure = invert_structure(self.save_structure[38][2])
                seen_challenges = {}
                for unlock in challenge_unlocks:
                    seen_challenges[unlock['name'].decode('latin1')] = True
                for challenge in sorted(self.challenges.values()):
                    if challenge.id_text not in seen_challenges:
                        player[38].append(
                            [
                                2,
                                write_protobuf(
                                    remove_structure(
                                        dict(
                                            [
                                                ('dlc_id', challenge.category.dlc),
                                                ('is_from_dlc', challenge.category.is_from_dlc),
                                                ('name', challenge.id_text),
                                            ]
                                        ),
                                        inverted_structure,
                                    )
                                ),
                            ]
                        )
            if 'ammo' in self.config.unlock:
                self.debug(' - Unlocking ammo capacity')
                s = read_repeated_protobuf_value(player[36][0][1], 0)
                for idx, (key, value) in enumerate(zip(self.black_market_keys, s)):
                    if key in self.black_market_ammo:
                        s[idx] = 7
                player[36][0][1] = write_repeated_protobuf_value(s, 0)

        # This should always come after the ammo-unlock section, since our
        # max ammo will change if more black market SDUs are unlocked.
        if self.config.maxammo is not None:
            self.debug(' - Setting ammo pools to maximum')

            # First we've gotta figure out our black market levels
            s = read_repeated_protobuf_value(player[36][0][1], 0)
            bm_levels = dict(zip(self.black_market_keys, s))

            # Make a dict of what our max ammo is for each of our black market
            # ammo pools
            max_ammo = {}
            for ammo_type, ammo_level in bm_levels.items():
                if ammo_type in self.black_market_ammo:
                    ammo_values = self.black_market_ammo[ammo_type]
                    if len(ammo_values) - 1 < ammo_level:
                        max_ammo[ammo_type] = (len(ammo_values) - 1, ammo_values[-1])
                    else:
                        max_ammo[ammo_type] = (ammo_level, ammo_values[ammo_level])

            # Now loop through our 'resources' structure and modify to
            # suit, updating 'amount' and 'level' as we go.
            inverted_structure = invert_structure(self.save_structure[11][2])
            seen_ammo = {}
            for idx, protobuf in enumerate(player[11]):
                data = apply_structure(read_protobuf(protobuf[1]), self.save_structure[11][2])
                resource = data['resource'].decode('latin1')
                if resource in self.ammo_resource_lookup:
                    ammo_type = self.ammo_resource_lookup[resource]
                    seen_ammo[ammo_type] = True
                    if ammo_type in max_ammo:
                        # Set the data in the structure
                        data['level'] = max_ammo[ammo_type][0]
                        data['amount'] = float(max_ammo[ammo_type][1])

                        # And now convert back into a protobuf
                        player[11][idx][1] = write_protobuf(remove_structure(data, inverted_structure))

                    else:
                        self.error(f'Ammo type "{ammo_type}" / pool "{data["pool"]}" not found!')
                else:
                    self.error(f'Ammo pool "{resource}" not found!')

            # Also, early in the game there isn't an entry in here for, for instance,
            # rocket launchers.  So let's make sure that all our known ammo exists.
            for ammo_type in bm_levels.keys():
                if ammo_type in self.ammo_resources.keys() and ammo_type not in seen_ammo:
                    new_struct = {
                        'resource': self.ammo_resources[ammo_type][0],
                        'pool': self.ammo_resources[ammo_type][1],
                        'level': max_ammo[ammo_type][0],
                        'amount': float(max_ammo[ammo_type][1]),
                    }
                    player[11].append([2, write_protobuf(remove_structure(new_struct, inverted_structure))])

        if self.config.challenges:
            data = self.unwrap_challenges(player[15][0][1])
            # You can specify multiple options at once.  Specifying "max" and
            # "bonus" at the same time, for instance, will put everything at its
            # max value, and then potentially lower the ones which have bonuses.
            do_zero = 'zero' in self.config.challenges
            do_max = 'max' in self.config.challenges
            do_bonus = 'bonus' in self.config.challenges

            if any([do_zero, do_max, do_bonus]):
                self.debug(' - Working with challenge data:')
                if do_zero:
                    self.debug('   - Setting challenges to 0')
                if do_max:
                    self.debug('   - Setting challenges to max-1')
                if do_bonus:
                    self.debug('   - Setting bonus challenges')

            for save_challenge in data['challenges']:
                if save_challenge['id'] in self.challenges:
                    if do_zero:
                        save_challenge['total_value'] = save_challenge['previous_value']
                    if do_max:
                        save_challenge['total_value'] = (
                            save_challenge['previous_value'] + self.challenges[save_challenge['id']].get_max()
                        )
                    if do_bonus and self.challenges[save_challenge['id']].bonus:
                        bonus_value = (
                            save_challenge['previous_value'] + self.challenges[save_challenge['id']].get_bonus()
                        )
                        if do_max or do_zero or save_challenge['total_value'] < bonus_value:
                            save_challenge['total_value'] = bonus_value

            player[15][0][1] = self.wrap_challenges(data)

        if self.config.fix_challenge_overflow:
            data = self.unwrap_challenges(player[15][0][1])

            for save_challenge in data['challenges']:
                if save_challenge['id'] in self.challenges:
                    if save_challenge['total_value'] >= 2000000000:
                        print(f'fix overflow in: {save_challenge["_name"]}')
                        save_challenge['total_value'] = self.challenges[save_challenge['id']].get_max() + 1

            player[15][0][1] = self.wrap_challenges(data)

        if self.config.name is not None and self.config.name:
            # TODO: move length check into config parsing
            self.debug(f' - Setting character name to "{self.config.name}"')
            data = apply_structure(read_protobuf(player[19][0][1]), self.save_structure[19][2])
            data['name'] = self.config.name
            player[19][0][1] = write_protobuf(remove_structure(data, invert_structure(self.save_structure[19][2])))

        if self.config.save_game_id is not None and self.config.save_game_id > 0:
            # TODO: move check into config parsing
            self.debug(f' - Setting save slot ID to {self.config.save_game_id}')
            player[20][0][1] = self.config.save_game_id

        return self.wrap_player_data(write_protobuf(player))

    def export_items(self, data, output):
        """
        Exports items stored in savegame data 'data' to the open
        filehandle 'output'
        """
        player = read_protobuf(self.unwrap_player_data(data))
        skipped_count = 0
        for i, name in ((41, "Bank"), (53, "Items"), (54, "Weapons")):
            count = 0
            content = player.get(i)
            if content is None:
                continue
            print(f'; {name}', file=output)
            for field in content:
                raw = read_protobuf(field[1])[1][0][1]

                # Borderlands uses some sort-of "fake" items to store some DLC
                # data.  As per the Gibbed sourcecode, this includes:
                #   1. "Currency On Hand"  (?)
                #   2. Last Playthrough Number / Playthroughs completed
                #   3. "Has played in UVHM"
                #   4. Overpower levels unlocked
                #   5. Last Overpower selection
                #
                # The data for these is stored in the `unknown2` field, by this
                # app's data definitions (or the protobuf's [2] index).  Regardless,
                # these aren't actual items, so we're skipping them.  See Gibbed's
                # Gibbed.Borderlands2.FileFormats/SaveExpansion.cs for details
                # on how to parse the `unknown2` field.
                is_weapon, item, key = self.unwrap_item(raw)
                if item[0] == 255 and not any([val != 0 for val in item[1:]]):
                    skipped_count += 1
                else:
                    count += 1
                    raw = replace_raw_item_key(raw, 0)
                    printable = base64.b64encode(raw).decode("latin1")
                    code = f'{self.item_prefix}({printable})'
                    print(code, file=output)
            self.debug(f' - {name} exported: {count}')
        # Don't bother reporting on skipped items, actually, since I now
        # know what they're actually used for.
        # self.debug(f' - Empty items skipped: {skipped_count}')

    def import_items(self, data, codelist):
        """
        Imports items into savegame data "data" based on the passed-in
        item list in "codelist"
        """
        player = read_protobuf(self.unwrap_player_data(data))

        prefix_length = len(self.item_prefix) + 1

        bank_count = 0
        weapon_count = 0
        item_count = 0

        to_bank = False
        for line in codelist.splitlines():
            line = line.strip()
            if line.startswith(";"):
                name = line[1:].strip().lower()
                if name == "bank":
                    to_bank = True
                elif name in ("items", "weapons"):
                    to_bank = False
                continue
            elif not (line.startswith(self.item_prefix + '(') and line.endswith(')')):
                continue

            code = line[prefix_length:-1]
            try:
                raw = base64.b64decode(code)
            except binascii.Error:
                continue

            key = random.randrange(0x100000000) - 0x80000000
            raw = replace_raw_item_key(raw, key)
            if to_bank:
                bank_count += 1
                field = 41
                entry = {1: [[2, raw]]}
            elif (raw[0] & 0x80) == 0:
                item_count += 1
                field = 53
                entry = {1: [[2, raw]], 2: [[0, 1]], 3: [[0, 0]], 4: [[0, 1]]}
            else:
                weapon_count += 1
                field = 54
                entry = {1: [[2, raw]], 2: [[0, 0]], 3: [[0, 1]]}

            player.setdefault(field, []).append([2, write_protobuf(entry)])

        self.debug(f' - Bank imported: {bank_count}')
        self.debug(f' - Items imported: {item_count}')
        self.debug(f' - Weapons imported: {weapon_count}')

        return self.wrap_player_data(write_protobuf(player))

    def __init__(
        self,
        *,
        args: List[str],
        item_struct_version: int,
        game_name: str,
        item_prefix: str,
        max_level: int,
        black_market_keys: Tuple[str, ...],
        black_market_ammo: Dict[str, List[int]],
        unlock_choices: List[str],
        levels_to_travel_station_map: Dict[str, str],
        no_exploration_challenge_levels: Set[str],
        challenges: Dict[int, Challenge],
    ) -> None:
        """
        Constructor.  Parses arguments and sets up our save_structure
        struct.
        """
        # B2 version is 7, TPS version is 10
        # "version" taken from what Gibbed calls it, not sure if that's
        # an appropriate descriptor or not.
        self.item_struct_version = item_struct_version

        self.game_name = game_name

        # Item export/import prefix
        self.item_prefix = item_prefix

        # Max char level
        self.max_level = max_level

        # The only difference here is that BLTPS has "laser"
        self.black_market_keys = black_market_keys

        # Dict to tell us which black market keys are ammo-related, and
        # what the max ammo is at each level.  Could be computed pretty
        # easily, but we may as well just store it.
        self.black_market_ammo = black_market_ammo

        # Available choices for --unlock option
        self.unlock_choices = unlock_choices

        # Level-to-Name Mapping
        self.levels_to_travel_station_map = levels_to_travel_station_map

        # Maps which don't actually contribute to the "Explorer-of-X" achievements
        self.no_exploration_challenge_levels = no_exploration_challenge_levels

        # There are two possible ways of uniquely identifying challenges in this file:
        # via their numeric position in the list, or by what looks like an internal
        # ID (though that ID is constructed a little weirdly, so I'm not sure if it's
        # actually intended to be used that way or not).
        #
        # I did run some tests, and it looks like internally, B2 probably does use
        # that ID field to identify the challenges...  You can mess around with the
        # order in which they're saved to the file, but so long as the ID field
        # is still pointing to the challenge you want, it'll be read in properly
        # (and then when you save your game, they'll be written back out in the
        # original order).
        #
        # Given that, I decided to go ahead and use that probably-ID field as the
        # index on this dict, rather than the order.  That should be slightly more
        # flexible for anyone editing the JSON directly, and theoretically
        # shouldn't be a problem in the future since there won't be any new major
        # DLC for B2...
        #
        # New major DLC for TPS seems unlikely too, though time will tell.
        self.challenges = challenges

        # Set up a reverse lookup for our ammo pools
        self.ammo_resource_lookup = {}
        for shortname, (resource, pool) in self.ammo_resources.items():
            self.ammo_resource_lookup[resource] = shortname

        # Parse Arguments
        self.config = self.parse_args(args)

        # Sets up our main save_structure var which controls how we read the file
        # This is implemented in AppBL2 and AppBLTPS
        self.save_structure = self.create_save_structure()

    def create_save_structure(self) -> Dict[int, Any]:
        raise NotImplementedError()

    def setup_game_specific_args(self, parser) -> None:
        """
        Function to add game-specific arguments.  By default it does nothing,
        must be overridden
        """
        pass

    def parse_args(self, argv):
        """
        Parse our arguments.
        """

        # Set up our config object
        config = Config()

        parser = argparse.ArgumentParser(
            description=f'Modify {self.game_name} Save Files',
            formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        )

        # Optional args

        parser.add_argument(
            '-o',
            '--output',
            choices=['savegame', 'decoded', 'decodedjson', 'json', 'items', 'none'],
            default='savegame',
            help="""
                    Output file format.  The most useful to humans are: savegame, json, and items.
                    If no output file is specified, this will revert to `none`.
                    """,
        )

        parser.add_argument(
            '-i',
            '--import-items',
            dest='import_items',
            help='read in codes for items and add them to the bank and inventory',
        )

        parser.add_argument(
            '-j',
            '--json',
            action='store_true',
            help='read savegame data from JSON format, rather than savegame',
        )

        parser.add_argument(
            '-b',
            '--bigendian',
            action='store_true',
            help='change the output format to big-endian, to write PS/xbox save files',
        )

        parser.add_argument(
            '-q',
            '--quiet',
            dest='verbose',
            action='store_false',
            help='quiet output (should generate no output unless there are errors)',
        )

        parser.add_argument(
            '-f',
            '--force',
            action='store_true',
            help='force output file overwrite, if the destination file exists',
        )

        # More optional args - used to be the "modify" option

        parser.add_argument(
            '--name',
            help='Set the name of the character',
        )

        parser.add_argument(
            '--save-game-id',
            dest='save_game_id',
            type=int,
            help='Set the save game slot ID of the character (probably not actually needed ever)',
        )

        parser.add_argument(
            '--level',
            type=int,
            help=f'Set the character to this level (from 1 to {self.max_level})',
        )

        parser.add_argument(
            '--money',
            type=int,
            help='Money to set for character',
        )

        # B2 and TPS have different currency types, so this function is
        # implemented in the implementing classes.
        self.setup_currency_args(parser)

        parser.add_argument(
            '--itemlevels',
            type=int,
            help='Set item levels (to set to current player level, specify 0).'
            'Skips level 1 items unless --forceitemlevels is specified too',
        )

        parser.add_argument(
            '--forceitemlevels',
            action='store_true',
            help='Set item levels even if the item is at level 1',
        )

        parser.add_argument(
            '--backpack',
            help=f'Set size of backpack (maximum is {self.max_backpack_size}, "max" may be specified)',
        )

        parser.add_argument(
            '--bank',
            help=f'Set size of bank (maximum is {self.max_bank_size}, "max" may be specified)',
        )

        parser.add_argument(
            '--gunslots',
            type=int,
            choices=[2, 3, 4],
            help='Set number of gun slots open',
        )

        parser.add_argument(
            '--copy-nvhm-missions',
            dest='copy_nvhm_missions',
            action='store_true',
            help='Copies NVHM mission state to both TVHM and UVHM modes.  Also unlocks TVHM/UVHM',
        )

        parser.add_argument(
            '--unlock',
            action=DictAction,
            choices=self.unlock_choices,
            default={},
            help='Game features to unlock',
        )

        parser.add_argument(
            '--challenges',
            action=DictAction,
            choices=['zero', 'max', 'bonus'],
            default={},
            help='Levels to set on challenge data',
        )

        parser.add_argument(
            '--maxammo',
            action='store_true',
            help='Fill all ammo pools to their maximum',
        )

        parser.add_argument(
            '--fix-challenge-overflow',
            action='store_true',
            help='Fix values for challenges which appear as huge negative numbers',
        )

        parser.add_argument(
            '--print-unexplored-levels',
            action='store_true',
            help='Print level names that are not fully explored by player',
        )

        # Positional args

        parser.add_argument('input_filename', help='Input filename, can be "-" to specify STDIN')

        parser.add_argument(
            'output_filename',
            nargs='?',
            help="""
                    Output filename, can be "-" to specify STDOUT.  Can be optional, in
                    which case no output file is produced.
                    """,
        )

        # Additional game-specific arguments
        self.setup_game_specific_args(parser)

        # Actually parse the args
        parser.parse_args(argv, config)

        # Do some extra fiddling
        config.finish(parser, self)

        # Some sanity checking with output type and output_filename
        if config.output_filename is None:
            # If we requested any changes, the only sensible course is to write them out
            if config.changes:
                parser.error("No output_filename was specified, but changes were requested")

            # If we manually specified an output type, we'll also need an output filename.
            # It's possible in this case that the user explicitly set `savegame` as the
            # output, rather than just leaving it at the default, but I don't think it's
            # worth the shenanigans necessary to detect that.
            if config.output not in {'savegame', 'none'}:
                parser.error(f"No output_filename was specified, but output type '{config.output}' was specified")

            # If we got here, we're probably good, but force ourselve to `none` output
            config.output = 'none'

        else:
            # If we have an output filename but `none` output, complain about it.
            if config.output == 'none':
                parser.error("Output filename specified but with `none` output")

        return config

    @staticmethod
    def notice(message) -> None:
        print(message)

    @staticmethod
    def error(message: str) -> None:
        print(f'ERROR: {message}', file=sys.stderr)

    def debug(self, message: str) -> None:
        if self.config.verbose:
            self.notice(message)

    def run(self):
        """
        Main routine - loads data, does things to it, and then writes
        out a file.
        """

        # Open up our input file
        self.debug('')
        if self.config.input_filename == '-':
            self.debug('Using STDIN for input file')
            input_file = sys.stdin
        else:
            self.debug(f'Opening {self.config.input_filename} for input file')
            input_file = open(self.config.input_filename, 'rb')
        self.debug('')

        # ... and read it in.
        save_data = input_file.read()
        if self.config.input_filename != '-':
            input_file.close()

        # If we're reading from JSON, convert it
        if self.config.json:
            self.debug('Interpreting JSON data')
            data = json.loads(save_data)
            if '1' not in data:
                # This means the file had been output as 'json'
                data = remove_structure(data, invert_structure(self.save_structure))
            save_data = self.wrap_player_data(write_protobuf(data))

        # If we've been told to import items, do so.
        if self.config.import_items:
            self.debug(f'Importing items from {self.config.import_items}')
            itemlist = open(self.config.import_items, 'r')
            save_data = self.import_items(save_data, itemlist.read())
            itemlist.close()

        # Now perform any changes, if requested
        if self.config.changes:
            self.debug('Performing requested changes')
            save_data = self.modify_save(save_data)

        # Show information if we've been passed any of those args
        if self.config.show_info:
            if self.config.changes:
                self.debug('')
            self.debug('Showing requested save information:')
            self.debug('')
            self.show_save_info(save_data)

        # If we have an output file, write to it!
        if self.config.output_filename is None:
            self.debug('No output file specified.  Exiting!')

        else:
            # Open our output file
            self.debug('')
            if self.config.output_filename == '-':
                self.debug('Using STDOUT for output file')
                output_file = sys.stdout
            else:
                self.debug(f'Opening {self.config.output_filename} for output file')
                if os.path.exists(self.config.output_filename):
                    if self.config.force:
                        self.debug(f'Overwriting output file "{self.config.output_filename}"')
                    else:
                        if self.config.input_filename == '-':
                            raise BorderlandsError(
                                f'Output filename "{self.config.output_filename}"'
                                + ' exists and --force not specified, aborting'
                            )
                        else:
                            self.notice('')
                            self.notice(f'Output filename "{self.config.output_filename}" exists')
                            sys.stderr.write('Continue and overwrite? [y|N] ')
                            sys.stderr.flush()
                            answer = sys.stdin.readline()
                            if answer[0].lower() == 'y':
                                self.notice('')
                                self.notice('Continuing!')
                            else:
                                self.notice('')
                                self.notice('Exiting!')
                                return
                if self.config.output == 'savegame' or self.config.output == 'decoded':
                    mode = 'wb'
                else:
                    mode = 'w'
                output_file = open(self.config.output_filename, mode)

            # Now output based on what we've been told to do
            if self.config.output == 'items':
                self.debug('Exporting items')
                self.export_items(save_data, output_file)
            elif self.config.output == 'savegame':
                self.debug('Writing savegame file')
                output_file.write(save_data)
            else:
                self.debug('Preparing decoded savegame file')
                player = self.unwrap_player_data(save_data)
                if self.config.output == 'decodedjson' or self.config.output == 'json':
                    self.debug('Converting to JSON for more human-readable output')
                    data = read_protobuf(player)
                    if self.config.output == 'json':
                        self.debug('Parsing protobuf data for even more human-readable output')
                        data = apply_structure(data, self.save_structure)
                    player = json.dumps(conv_binary_to_str(data), sort_keys=True, indent=4)
                self.debug('Writing decoded savegame file')
                output_file.write(player)

            # Close the output file
            if self.config.output_filename != '-':
                output_file.close()

        # ... aaand we're done.
        self.debug('')
        self.debug('Done!')

    @staticmethod
    def setup_currency_args(parser) -> None:
        raise NotImplementedError()
