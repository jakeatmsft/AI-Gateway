import copy
import json
from pathlib import Path
from xml.etree import ElementTree

import pytest

from filtering import DomainRedactor, InvalidRequest, domain_list
from processing import invoked_web_search, metric_headers, response_events


@pytest.mark.parametrize(('original', 'expected'), [
    ('Keep  https://youtube.com/a?q=1#x  words.\n', 'Keep  [BLOCKED LINK]  words.\n'),
    ('[video](https://youtube.com/a)', '[video]([BLOCKED LINK])'),
    ('[video](youtube.com)', '[video]([BLOCKED LINK])'),
    ('[video](https://youtube.com/a "title")', '[video]([BLOCKED LINK] "title")'),
    ('[video](<https://youtube.com/a>)', '[video](<[BLOCKED LINK]>)'),
    ('<a href="https://YOUTUBE.com/a"><b>video</b></a>', '<a href="[BLOCKED LINK]"><b>video</b></a>'),
    ('<a href="youtube.com">video</a>', '<a href="[BLOCKED LINK]">video</a>'),
    ('<https://youtube.com/a>', '<[BLOCKED LINK]>'),
    ('<youtube.com>', '<[BLOCKED LINK]>'),
    ('(https://news.youtube.com./a).', '([BLOCKED LINK]).'),
    ('https://good.example/a?q=youtube.com/x', 'https://good.example/a?q=youtube.com/x'),
    ('https://notyoutube.com/a', 'https://notyoutube.com/a'),
    ('https://youtube.com.good.example/a', 'https://youtube.com.good.example/a'),
    ('www.youtube.com/a and youtube.com/path', '[BLOCKED LINK] and [BLOCKED LINK]'),
    ('//youtube.com/a', '[BLOCKED LINK]'),
    ('[video][ref]\n[ref]: https://youtube.com/a\n', '[video][ref]\n[ref]: [BLOCKED LINK]\n'),
    ('[ref]: youtube.com "title"\n', '[ref]: [BLOCKED LINK] "title"\n'),
    ('`https://youtube.com/a`', '`[BLOCKED LINK]`'),
    ('https://good.example/文档?q=1#x\n\t  ', 'https://good.example/文档?q=1#x\n\t  '),
    ('[https://youtube.com/a](https://youtube.com/a)', '[[BLOCKED LINK]]([BLOCKED LINK])'),
    ('[https://youtube.com](https://good.example)', '[[BLOCKED LINK]](https://good.example)'),
    ('https://user:pass@youtube.com:443/a', '[BLOCKED LINK]'),
    ('https://youtube%2ecom/a', '[BLOCKED LINK]'),
    ('https://%79%6f%75tube.com/a', '[BLOCKED LINK]'),
    ('<a href="https://youtube&#46;com/a">label</a>', '<a href="[BLOCKED LINK]">label</a>'),
    ('https://youtube.com/a_(b).', '[BLOCKED LINK].'),
    ('[label](https://youtube.com/a_(b))', '[label]([BLOCKED LINK])'),
    ('The domain youtube.com is mentioned in prose.', 'The domain youtube.com is mentioned in prose.'),
    ('https://youtube.com\\path', '[BLOCKED LINK]'),
    ('https://youtube.com. Next.', '[BLOCKED LINK]. Next.'),
])
def test_redaction_replaces_whole_blocked_urls(original, expected):
    redactor = DomainRedactor(['youtube.com'])
    actual = redactor.text(original)
    assert actual == expected
    assert redactor.text(actual) == actual


def test_domains_are_literal_regex_patterns_with_hostname_boundaries():
    redactor = DomainRedactor(['bad.example', 'repo.maven.apache.org'])
    assert redactor.text('https://badXexample/a https://notbad.example/a') == 'https://badXexample/a https://notbad.example/a'
    assert redactor.text('https://sub.bad.example/a') == '[BLOCKED LINK]'
    assert redactor.text('https://repo.maven.apache.org/x') == '[BLOCKED LINK]'


def test_idna_and_case_normalization():
    domains = domain_list(['BÜCHER.example.', 'xn--bcher-kva.example'])
    assert domains == ['xn--bcher-kva.example']
    redactor = DomainRedactor(domains)
    assert redactor.text('https://bücher.example/a') == '[BLOCKED LINK]'
    assert redactor.text('https://xn--bcher-kva.example/a') == '[BLOCKED LINK]'


@pytest.mark.parametrize('field', ['url', 'uri', 'href', 'src'])
def test_structured_urls_remove_credentials_port_path_query_and_fragment(field):
    value = {field: 'https://user:secret@news.youtube.com:8443/private?q=secret#tail!'}
    assert DomainRedactor(['youtube.com']).response(value) == {field: '[BLOCKED LINK]'}


def test_multiple_replacements_preserve_punctuation_and_allowed_links():
    original = ('Read (https://youtube.com/a?q=1#x), https://good.example/a?q=youtube.com; '
                '[video](https://youtube.com/b "Title").\n')
    expected = ('Read ([BLOCKED LINK]), https://good.example/a?q=youtube.com; '
                '[video]([BLOCKED LINK] "Title").\n')
    assert DomainRedactor(['youtube.com']).text(original) == expected


@pytest.mark.parametrize('bad', [None, 'youtube.com', [''], ['https://youtube.com'], ['*.youtube.com'],
                                     ['youtube.com/path'], [1], [' youtube.com'], ['bad..example']])
def test_invalid_domain_lists(bad):
    with pytest.raises(InvalidRequest):
        domain_list(bad)


def document():
    text = 'Read https://youtube.com/a and docs.'
    return {'id': 'resp_original', 'status': 'completed', 'model': 'deployment', 'object': 'response',
            'created_at': 1, 'output': [
                {'id': 'ws_original', 'type': 'web_search_call', 'status': 'completed',
                 'action': {'sources': [{'url': 'https://youtube.com/source'}, {'url': 'https://good.example/a'}]}},
                {'id': 'msg_original', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                 'content': [{'type': 'output_text', 'text': text, 'annotations': [
                     {'type': 'url_citation', 'url': 'https://youtube.com/a', 'title': 'video',
                      'start_index': 5, 'end_index': 26}]}]}],
            'tool_usage': {'web_search': {'num_requests': 2}},
            'usage': {'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15}}


def test_full_response_redaction_preserves_ids_usage_metadata_and_citation_offsets():
    original = document()
    saved = copy.deepcopy(original)
    result = DomainRedactor(['youtube.com']).response(original)
    assert original == saved
    assert result['id'] == original['id'] and result['usage'] == original['usage']
    part = result['output'][1]['content'][0]
    assert part['text'] == 'Read [BLOCKED LINK] and docs.'
    annotation = part['annotations'][0]
    assert annotation['url'] == '[BLOCKED LINK]'
    assert annotation['start_index'] == 5 and annotation['end_index'] == 19
    assert result['output'][0]['action']['sources'] == [
        {'url': '[BLOCKED LINK]'}, {'url': 'https://good.example/a'}]
    assert json.loads(metric_headers(result)['x-response-metrics']) == {'web_search_count': 2, 'total_tokens': 15}


@pytest.mark.parametrize(('domain', 'url', 'replacement'), [
    ('youtube.com', 'https://youtube.com/a', '[BLOCKED LINK]'),
    ('bad.example', 'https://bad.example/a', '[BLOCKED LINK]'),
    ('longdomain.example', 'https://longdomain.example/a', '[BLOCKED LINK]'),
    ('youtube.com', 'https://%79%6f%75tube.com/a', '[BLOCKED LINK]'),
    ('x.ai', 'https://x.ai/a', '[BLOCKED LINK]'),
    ('ab.example', 'https://ab.example/a', '[BLOCKED LINK]'),
])
def test_replacement_length_changes_keep_citation_ranges_aligned(domain, url, replacement):
    text = f'A {url} Z'
    part = {'type': 'output_text', 'text': text, 'annotations': [
        {'type': 'url_citation', 'url': url, 'start_index': 2, 'end_index': 2 + len(url)},
        {'type': 'url_citation', 'url': 'https://good.example/',
         'start_index': len(text) - 1, 'end_index': len(text)},
    ]}
    result = DomainRedactor([domain]).response(part)
    assert result['text'] == f'A {replacement} Z'
    link, following = result['annotations']
    assert link['url'] == replacement
    assert (link['start_index'], link['end_index']) == (2, 2 + len(replacement))
    assert result['text'][following['start_index']:following['end_index']] == 'Z'
    assert DomainRedactor([domain]).response(result) == result


@pytest.mark.parametrize('status', ['in_progress', 'searching', 'completed', 'failed'])
def test_actual_tool_item_triggers_detection_regardless_of_call_status(status):
    assert invoked_web_search({'output': [{'type': 'web_search_call', 'status': status}]})


@pytest.mark.parametrize('response', [None, {}, {'output': []},
    {'tools': [{'type': 'web_search'}], 'output': [{'type': 'message'}]},
    {'output': [{'type': 'function_call', 'name': 'web_search'}]},
    {'tool_usage': {'web_search': {'num_requests': 2}}, 'output': []}])
def test_offering_or_mentioning_web_search_does_not_trigger_redaction(response):
    assert not invoked_web_search(response)


async def test_stream_preserves_multiple_items_parts_and_the_redacted_terminal_response():
    initial = document()
    initial['output'][1]['content'].append({'type': 'output_text', 'text': 'Second https://youtube.com/part', 'annotations': []})
    initial['output'].append({'id': 'msg_second', 'type': 'message', 'role': 'assistant', 'status': 'completed',
                              'content': [{'type': 'output_text', 'text': 'Third https://good.example/', 'annotations': []}]})
    result = DomainRedactor(['youtube.com']).response(initial)
    frames = [frame async for frame in response_events(result)]
    wire = b''.join(frames)
    assert b'youtube.com' not in wire and b'[BLOCKED LINK]' in wire
    events = [json.loads(frame.decode().split('data: ', 1)[1]) for frame in frames]
    assert [e['sequence_number'] for e in events] == list(range(len(events)))
    assert events[-1]['response'] == result and events[-1]['type'] == 'response.completed'
    annotation_events = [e for e in events if e['type'] == 'response.output_text.annotation.added']
    assert annotation_events[0]['annotation'] == result['output'][1]['content'][0]['annotations'][0]
    added_parts = [e['part'] for e in events if e['type'] == 'response.content_part.added']
    assert all(part['text'] == '' and part['annotations'] == [] for part in added_parts)
    for output_index, item in enumerate(result['output']):
        if item['type'] != 'message':
            continue
        for content_index, part in enumerate(item['content']):
            text = ''.join(e['delta'] for e in events if e['type'] == 'response.output_text.delta'
                           and e['output_index'] == output_index and e['content_index'] == content_index)
            assert text == part['text']


def test_apim_streams_through_relay_and_inspects_only_json_for_conditional_redaction():
    policy = ElementTree.parse(Path(__file__).parents[1] / 'policy.xml')
    detection = policy.find('.//set-variable[@name="usedWebSearch"]').attrib['value']
    assert '"web_search_call"' in detection
    assert '"completed"' not in detection
    branches = policy.findall('./inbound/choose/when/choose/when')
    bypass = next(branch for branch in branches if 'usedWebSearch' in branch.attrib.get('condition', ''))
    assert '!(bool)' in bypass.attrib['condition']
    assert bypass.find('return-response').attrib['response-variable-name'] == 'initial'
    assert policy.find('.//rewrite-uri[@template="/api/redact"]') is not None
    assert policy.find('.//set-backend-service[@base-url="{foundry-endpoint}"]') is not None
    relay = next(branch for branch in policy.findall('./inbound/choose/when')
                 if branch.find('rewrite-uri[@template="/api/responses"]') is not None)
    assert 'isStreaming' in relay.attrib['condition'] and 'hasWebSearch' in relay.attrib['condition']
    assert relay.find('send-request') is None
    assert policy.find('./backend/forward-request').attrib['buffer-response'] == 'false'
    parser = policy.find('.//set-variable[@name="initialResponse"]').attrib['value']
    assert 'application/json' in parser and 'text/event-stream' not in parser
