import re
import sys
from typing import List, Optional, Final

from borderlands.bl2_skill_data import CHAR_SKILLS, SkillItem

_MISNAME_FIXES: Final = {
    'shockandaaagggghhh': 'shockandaaaggghhh',
    'divergentlikeness': 'divergentlikness',
    'yippiekiyay': 'yippeekiyay',
    'alloutofbubblegum': 'outofbubblegum',
    # Commando
    'hardtokill': 'diehard',
    'maglock': 'mag-lock',
    # Zer0
    'headsh0t': 'headshot',
    '0ptics': 'optics',
    'precisi0n': 'precision',
    '0nesh0t0nekill': 'oneshotonekill',
    'b0re': 'bore',
    'vel0city': 'velocity',
    'killc0nfirmed': 'killconfirmed',
    'at0newiththegun': 'atonewiththegun',
    'criticalascensi0n': 'criticalascention',
    'c0unterstrike': 'counterstrike',
    'risingsh0t': 'risingshot',
    'unf0rseen': 'unforseen',
    'tw0fang': 'twofang',
    'deathbl0ss0m': 'deathblossom',
    'killingbl0w': 'killingblow',
    'ir0nhand': 'ironhand',
    'f0ll0wthr0ugh': 'followthrough',
    # psycho
    'bloodtwitch': 'bloodytwitch',
    'buzzaxebombardier': 'buzzaxebombadier',
    'emptytherage': 'emptyrage',
}
_CHAR_NAME_FIXES: Final = {
    'mercenary': 'gunzerker',
    'soldier': 'commando',
    'lilacplayerclass': 'psycho',
}


def make_skills_string(skill_records: List[SkillItem], skill_data: List[dict]) -> str:
    values = []
    data = list(skill_data)

    prev_branch_name = ''
    for name, max_value in skill_records:
        clean_name = re.sub(r'[\' \-!\",]', '', name).replace('%', 'percent').lower()
        clean_name = _MISNAME_FIXES.get(clean_name, clean_name)

        found_index: Optional[int] = None
        for i, item in enumerate(data):
            if item['name'].decode().lower().endswith('.' + clean_name):
                found_index = i
                break
        if found_index is None:
            sys.exit('unable to find value for skill %r (%r)' % (name, clean_name))
        value = data[found_index]['level']
        skill_name = data[found_index]['name'].decode()
        branch_name, skill = skill_name.split('.')[-2:]
        if branch_name != prev_branch_name:
            print('[%s]' % branch_name)
            prev_branch_name = branch_name
        print('%d - %s' % (value, skill))
        data.pop(found_index)
        if value < 0:
            sys.exit('%r/%r: negative value %d' % (name, clean_name, value))
        if value > max_value:
            sys.exit('%r/%r: value overflow: %d (max is %d)' % (name, clean_name, value, max_value))
        values.append(value)

    return ''.join(str(x) for x in values)


def make_bl2skills_link(json_data: dict) -> str:
    data_class = json_data['class'].decode()
    char_name = data_class.split('_')[-1].lower()
    char_name = _CHAR_NAME_FIXES.get(char_name, char_name)
    if char_name not in CHAR_SKILLS:
        sys.exit('unknown character class in save file: %r (%r)' % (char_name, data_class))
    skill_records = CHAR_SKILLS[char_name]
    skills_string = make_skills_string(skill_records, json_data['skills'])
    return 'https://bl2skills.com/%s.html#%s' % (char_name, skills_string)
