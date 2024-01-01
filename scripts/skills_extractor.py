"""
Utility for extracting skill names from html files from:
  https://github.com/seigler/bl2skills.com

Note: this is source of https://bl2skills.com/
"""
import os
import pprint
import re
import sys
from typing import List, Tuple

SkillItem = Tuple[str, int]
SKILL_PARTS_RE = re.compile(
    r'<div class="[^"]+" data-points="[^"]+" data-max="(\d)">.+?<h2>(.+?)</h2>', re.IGNORECASE | re.DOTALL
)


def extract_skills(filename: str) -> List[SkillItem]:
    with open(filename, encoding='utf-8') as inp:
        data = inp.read()

    result: List[SkillItem] = []
    records = SKILL_PARTS_RE.findall(data)
    for rec in records:
        result.append((rec[1], int(rec[0])))
    return result


def main() -> None:
    names = """
    assassin.html
    commando.html
    gunzerker.html
    mechromancer.html
    psycho.html
    siren.html
    """.split()

    result = {}
    for filename in names:
        char_name = filename.split('.')[0]
        result[char_name] = extract_skills(filename)

    outfile = 'bl2_skill_data.py'
    if os.path.exists(outfile):
        sys.exit('file already exists: %r' % (outfile,))
    with open(outfile, 'w', encoding='utf-8') as out:
        print('from typing import Tuple, Dict, List', file=out)
        print('', file=out)
        print('SkillItem = Tuple[str, int]', file=out)
        print('CHARACTER_SKILLS: Final[Dict[str, List[SkillItem]]] = ', end='', file=out)
        pprint.pprint(result, stream=out)


if __name__ == '__main__':
    main()
