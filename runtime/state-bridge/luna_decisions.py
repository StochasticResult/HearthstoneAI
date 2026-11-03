"""One typed choice from the same public evidence used by the Sol advisor."""
import json
import math

from openai_advisor import AdvisorError, ContextTooLarge, MAX_INPUT_BYTES

MODEL = 'gpt-6-luna'


def decision_payload(responses_payload, menu):
    if not 2 <= len(menu) <= 255:
        raise AdvisorError('Decisions requires 2–255 legal actions')
    # Keep the gameplay instructions and identical serialized board. Only
    # the return protocol differs: this endpoint has no function calls.
    instructions = responses_payload['instructions'].replace(
        'using choose_action.', 'using the supplied choices.').replace(
        'Return only the function call. Give a concise Chinese reason naming the decisive fact and\n'
        'purpose (not a long thought process), then an action_id matching that final assessment.',
        'Select the action ID that matches your assessment.')
    choices = [{'value': aid, 'description': json.dumps(action, separators=(',', ':'))}
               for aid, action in menu.items()]
    payload = {'model': MODEL, 'input': responses_payload['input'],
               'questions': [{'name':'next_action', 'type':'choice',
                              'instructions': instructions, 'choices': choices}]}
    if len(json.dumps(payload, separators=(',', ':')).encode()) > MAX_INPUT_BYTES:
        raise ContextTooLarge('Decisions input exceeds the request size limit')
    return payload


def parse_choice(reply, menu):
    model = reply.get('model')
    if not isinstance(model, str) or not (model == MODEL or model.startswith(MODEL+'-')):
        raise AdvisorError('Decisions returned an unexpected model')
    answers = reply.get('answers')
    if not isinstance(answers, list) or len(answers) != 1 or not isinstance(answers[0], dict):
        raise AdvisorError('Decisions did not return one answer')
    a = answers[0]
    aid, confidence = a.get('choice'), a.get('confidence')
    if a.get('type') != 'choice' or a.get('name') != 'next_action' or type(aid) is not str or aid not in menu:
        raise AdvisorError('Decisions did not return a choice in the current menu')
    if type(confidence) not in (int, float) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise AdvisorError('Invalid Decisions confidence')
    usage = reply.get('usage')
    if not isinstance(usage, dict) or type(usage.get('total_tokens')) is not int or usage['total_tokens'] < 0:
        raise AdvisorError('Decisions token accounting is missing')
    return dict(menu[aid]), confidence, usage['total_tokens']
