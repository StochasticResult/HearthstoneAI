"""One shared public card catalog, without duplicate multi-megabyte loads."""
import json
import sys
from pathlib import Path

CATALOG = {} if sys.argv[1:] == ['--worker'] else json.loads(Path(__file__).with_name('card-catalog.json').read_text())
PUBLIC_OBJECTIVES = frozenset(cid for cid, card in CATALOG.items()
                             if set(card.get('mechanics',[])) & {'QUEST','SIDE_QUEST','QUESTLINE','OBJECTIVE'})
RITUALS = frozenset({'BE_005', 'BE_085', 'BE_726', 'BE_090t1', 'BE_090t2', 'BE_090t3',
                     'CAP_405tb2', 'CAP_405tb3'})


def is_secret_card(card_id):
    card = CATALOG.get(card_id, {})
    return 'SECRET' in card.get('mechanics', ()) or card.get('text', '').lstrip().startswith('Secret:')
