"""One shared public card catalog, without duplicate multi-megabyte loads."""
import json
import sys
from pathlib import Path

CATALOG = {} if sys.argv[1:] == ['--worker'] else json.loads(Path(__file__).with_name('card-catalog.json').read_text())
PUBLIC_OBJECTIVES = frozenset(cid for cid, card in CATALOG.items()
                             if set(card.get('mechanics',[])) & {'QUEST','SIDE_QUEST','QUESTLINE','OBJECTIVE'})
RITUALS = frozenset({'BE_085','BE_726','CAP_405tb2','CAP_405tb3'})
